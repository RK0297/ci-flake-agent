"""
src/analyze.py - LLM-driven log parsing and flaky failure categorization.
"""

import os
import sys
import re
import json
import argparse
import requests


# Strict category constants required by system design
VALID_CATEGORIES = ["race_condition", "network_timeout", "infra_failure", "real_bug"]


def smart_truncate_log(log_text: str, head_lines: int = 30, tail_lines: int = 200) -> str:
    """
    SMART LOG TRUNCATION FOR LLM CONTEXT WINDOW OPTIMIZATION:
    1. Keeps first `head_lines` (environment setup, execution context).
    2. Identifies lines containing error signals / tracebacks and includes surrounding context.
    3. Keeps last `tail_lines` (execution summary, exit codes).
    4. Merges ranges to produce a compact, high-signal prompt.
    """
    lines = log_text.splitlines()
    total_lines = len(lines)

    if total_lines <= (head_lines + tail_lines + 50):
        return log_text

    selected_indices = set(range(min(head_lines, total_lines)))
    selected_indices.update(range(max(0, total_lines - tail_lines), total_lines))

    # Error keywords to extract surrounding context
    error_pattern = re.compile(
        r'(error|fail|failed|exception|traceback|timeout|connectionerror|assertionerror|memoryerror|oom)',
        re.IGNORECASE
    )

    for i, line in enumerate(lines):
        if error_pattern.search(line):
            # Include 5 lines before and 10 lines after error
            start = max(0, i - 5)
            end = min(total_lines, i + 11)
            selected_indices.update(range(start, end))

    sorted_indices = sorted(selected_indices)
    compact_lines = []
    prev_idx = -1

    for idx in sorted_indices:
        if prev_idx != -1 and idx > prev_idx + 1:
            compact_lines.append(f"... [truncated {idx - prev_idx - 1} lines of normal output] ...")
        compact_lines.append(lines[idx])
        prev_idx = idx

    return "\n".join(compact_lines)


def parse_llm_json(response_text: str) -> dict:
    """Extract and validate JSON payload from LLM response string."""
    if not response_text:
        return None
        
    # Match JSON block or object between braces
    match = re.search(r'\{[^{}]*"category"[^{}]*\}', response_text, re.DOTALL)
    if not match:
        match = re.search(r'\{.*\}', response_text, re.DOTALL)

    json_str = match.group(0) if match else response_text.strip()
    
    try:
        data = json.loads(json_str)
        cat = str(data.get("category", "")).lower().replace(" ", "_")
        
        # Ensure category maps to strict enum
        if cat not in VALID_CATEGORIES:
            if "timeout" in cat or "network" in cat:
                cat = "network_timeout"
            elif "race" in cat or "state" in cat or "concurrent" in cat:
                cat = "race_condition"
            elif "infra" in cat or "conn" in cat or "service" in cat:
                cat = "infra_failure"
            else:
                cat = "real_bug"
                
        return {
            "category": cat,
            "confidence": float(data.get("confidence", 0.90)),
            "explanation": str(data.get("explanation", data.get("summary", ""))),
            "suggested_mitigation": str(data.get("suggested_mitigation", data.get("mitigation", "")))
        }
    except Exception:
        return None



def call_ollama(log_snippet: str, model: str = None) -> dict:
    """Call local Ollama AI endpoint (http://localhost:11434) with auto-detected installed model."""
    tags_url = "http://localhost:11434/api/tags"
    generate_url = "http://localhost:11434/api/generate"

    selected_model = model
    if not selected_model:
        try:
            r = requests.get(tags_url, timeout=2)
            if r.status_code == 200:
                models = [m['name'] for m in r.json().get('models', [])]
                if models:
                    selected_model = models[0] # Use first available installed model
        except Exception:
            pass

    if not selected_model:
        selected_model = "gemma3:1b"

    print(f"[LLM] Querying local Ollama AI model '{selected_model}'...")

    prompt = f"""You are a CI/CD Reliability Engineer analyzing a flaky build log.
Categorize the failure into EXACTLY ONE of these categories:
- race_condition
- network_timeout
- infra_failure
- real_bug

Respond strictly in valid JSON format:
{{
  "category": "race_condition|network_timeout|infra_failure|real_bug",
  "confidence": 0.95,
  "explanation": "<2-3 sentence root cause breakdown>",
  "suggested_mitigation": "<Actionable fix recommendation>"
}}

Log Content:
{log_snippet}
"""

    try:
        res = requests.post(generate_url, json={"model": selected_model, "prompt": prompt, "stream": False}, timeout=30)
        if res.status_code == 200:
            raw_response = res.json().get("response", "")
            parsed = parse_llm_json(raw_response)
            if parsed:
                parsed["llm_provider"] = f"ollama ({selected_model})"
                return parsed
    except Exception as e:
        print(f"[NOTICE] Ollama local API call failed ({e}).", file=sys.stderr)
    return None



def rule_based_analysis(log_text: str) -> dict:
    """Rule-based heuristic fallback analyzer matching strict categories."""
    log_lower = log_text.lower()
    
    if "timeouterror" in log_lower or "timed out" in log_lower or "5000ms" in log_lower or "lock acquisition" in log_lower:
        category = "network_timeout"
        explanation = "Operation timed out waiting for lock release or network response within threshold."
        mitigation = "Increase async timeout threshold or implement exponential backoff retry policy."
        confidence = 0.92
    elif "connectionerror" in log_lower or "connection refused" in log_lower or "404" in log_lower:
        category = "infra_failure"
        explanation = "Network connection refused or target external service endpoint was unreachable."
        mitigation = "Verify network routing, use a mock server fixture, or add automatic network retries."
        confidence = 0.95
    elif "assertionerror" in log_lower or "race condition" in log_lower or "expected balance" in log_lower:
        category = "race_condition"
        explanation = "Non-deterministic state mismatch detected across concurrent worker threads."
        mitigation = "Isolate shared state variables, use mutex locks, or execute test suite with atomic fixtures."
        confidence = 0.89
    elif "memoryerror" in log_lower or "oom" in log_lower or "exit code 137" in log_lower:
        category = "real_bug"
        explanation = "Out of Memory (OOM) error or process termination caused by excessive resource allocation."
        mitigation = "Optimize array memory allocations, increase runner RAM limits, or profile memory usage."
        confidence = 0.94
    else:
        category = "real_bug"
        explanation = "Unclassified test failure detected in workflow execution."
        mitigation = "Inspect raw stack trace for unhandled exceptions or code bugs."
        confidence = 0.70

    return {
        "category": category,
        "confidence": confidence,
        "explanation": explanation,
        "suggested_mitigation": mitigation
    }


def categorize_flake(log_text: str, provider: str = "auto") -> dict:
    """Main log analysis dispatcher supporting LLM with heuristic fallback."""
    truncated_log = smart_truncate_log(log_text)

    # 1. Try Ollama local AI if requested or in auto mode
    if provider in ("auto", "ollama"):
        result = call_ollama(truncated_log)
        if result:
            return result

    # 2. Heuristic fallback
    return rule_based_analysis(log_text)


def analyze_file(input_path: str, output_path: str, provider: str = "auto"):
    """Analyze a single log file and write JSON output."""
    with open(input_path, "r", encoding="utf-8") as f:
        log_text = f.read()

    result = categorize_flake(log_text, provider)
    result["input_log"] = os.path.basename(input_path)

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[+] Analyzed {os.path.basename(input_path)} -> Category: '{result['category']}' (Confidence: {result['confidence'] * 100:.1f}%)")
    return result


def main():
    parser = argparse.ArgumentParser(description="Analyze CI logs for flaky test failures using LLM & Smart Truncation.")
    parser.add_argument("--input", required=True, help="Path to input log file or logs/ directory")
    parser.add_argument("--output", default="reports/analysis.json", help="Path to save JSON analysis result")
    parser.add_argument("--provider", default="auto", choices=["auto", "ollama", "heuristic"], help="LLM provider choice")

    args = parser.parse_args()

    if os.path.isdir(args.input):
        os.makedirs(args.output if args.output.endswith("/") or not os.path.splitext(args.output)[1] else "reports", exist_ok=True)
        log_files = [os.path.join(args.input, f) for f in os.listdir(args.input) if f.endswith(".txt")]
        print(f"[INFO] Batch analyzing {len(log_files)} log file(s) in '{args.input}'...")
        
        batch_results = []
        for file_path in log_files:
            out_name = f"analysis_{os.path.basename(file_path).replace('.txt', '')}.json"
            out_path = os.path.join("reports", out_name)
            res = analyze_file(file_path, out_path, args.provider)
            batch_results.append(res)
            
        summary_path = os.path.join("reports", "batch_analysis.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(batch_results, f, indent=2)
        print(f"[SUCCESS] Batch analysis complete. Summary saved to {summary_path}")
    else:
        if not os.path.exists(args.input):
            print(f"Error: Input file {args.input} does not exist.", file=sys.stderr)
            sys.exit(1)
        analyze_file(args.input, args.output, args.provider)


if __name__ == "__main__":
    main()
