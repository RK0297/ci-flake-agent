"""
src/fetch_logs.py - Fetch GitHub Actions workflow logs via GitHub REST API and detect flaky runs.
"""

import os
import sys
import re
import io
import json
import zipfile
import argparse
import subprocess
import requests


def get_auth_token(provided_token: str = None) -> str:
    """Retrieve GitHub token from argument, environment variable, or git credentials helper."""
    if provided_token:
        return provided_token
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    # Try git credential helper fallback
    try:
        proc = subprocess.run(
            ['git', 'credential', 'fill'],
            input=b'protocol=https\nhost=github.com\n',
            capture_output=True,
            timeout=5
        )
        for line in proc.stdout.decode('utf-8', errors='ignore').splitlines():
            if line.startswith('password='):
                return line.split('=', 1)[1]
    except Exception:
        pass
    return ""


def get_headers(token: str = "") -> dict:
    """Build GitHub API HTTP request headers."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ci-flake-agent/1.0"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_failed_runs(repo: str, token: str = "") -> list:
    """
    Fetch all failed workflow runs for a repository via GitHub REST API:
    GET /repos/{owner}/{repo}/actions/runs?status=failure
    """
    headers = get_headers(token)
    url = f"https://api.github.com/repos/{repo}/actions/runs?status=failure&per_page=100"
    print(f"[INFO] Fetching failed workflow runs for '{repo}'...")
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"[ERROR] API request failed with status code {response.status_code}: {response.text}", file=sys.stderr)
        return []

    data = response.json()
    failed_runs = data.get("workflow_runs", [])
    print(f"[INFO] Found {len(failed_runs)} failed workflow run(s).")
    return failed_runs


def clean_log_text(raw_text: str) -> str:
    """
    Strip ISO-8601 timestamps, ANSI color codes, and GitHub runner noise.
    """
    cleaned_lines = []
    for line in raw_text.splitlines():
        # Strip ISO-8601 timestamp at beginning of line (e.g., 2026-08-01T18:31:00.1234567Z )
        line = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*', '', line)
        # Strip ANSI escape sequences
        line = re.sub(r'\x1b\[[0-9;]*[mGKH]', '', line)
        line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
        
        # Skip empty or runner-internal noise lines
        if not line.strip():
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def fetch_and_extract_run_logs(repo: str, run_id: int, output_dir: str = "logs", token: str = "") -> list:
    """
    Download log zip via GitHub API:
    GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs
    Extract failing step logs, clean noise, and save to output_dir.
    """
    headers = get_headers(token)
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"
    print(f"[INFO] Downloading log archive for run #{run_id}...")

    # First request: handle 302 redirect to download URL
    res = requests.get(url, headers=headers, allow_redirects=False)
    if res.status_code in (301, 302, 307):
        download_url = res.headers["Location"]
        # Download actual zip archive without Authorization header to avoid S3 403 error
        zip_res = requests.get(download_url)
    elif res.status_code == 200:
        zip_res = res
    else:
        print(f"[ERROR] Failed to fetch log zip for run #{run_id} (Status {res.status_code})", file=sys.stderr)
        return []

    os.makedirs(output_dir, exist_ok=True)
    extracted_files = []

    try:
        z = zipfile.ZipFile(io.BytesIO(zip_res.content))
        for filename in z.namelist():
            # Target step log files (ignore top-level summary files or non-txt entries)
            if filename.endswith(".txt") and "/" in filename:
                raw_text = z.read(filename).decode("utf-8-sig", errors="ignore")
                
                # Check if this log file contains error or failure indicators
                log_lower = raw_text.lower()
                is_failing_step = any(keyword in log_lower for keyword in [
                    "error", "fail", "failed", "exception", "traceback", "exit code", "timeout"
                ])
                
                if is_failing_step:
                    cleaned = clean_log_text(raw_text)
                    step_slug = re.sub(r'[^\w\-]', '_', filename.split("/")[-1].replace(".txt", ""))
                    out_path = os.path.join(output_dir, f"run_{run_id}_{step_slug}.txt")
                    
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(cleaned)
                    
                    extracted_files.append(out_path)
                    print(f"  -> Extracted failing step log: {out_path}")
    except zipfile.BadZipFile:
        print(f"[ERROR] Downloaded payload for run #{run_id} was not a valid zip file.", file=sys.stderr)

    return extracted_files


def detect_flaky_runs(repo: str, token: str = "") -> dict:
    """
    CORE FLAKE DETECTION LOGIC:
    Identifies "flaky" runs where the same workflow has BOTH passing (success) and
    failing (failure) runs on the exact same commit SHA (or retry attempt succeeded).
    """
    headers = get_headers(token)
    url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=100"
    print(f"\n[INFO] Running Flaky Test Detector for '{repo}'...")

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"[ERROR] Failed to fetch workflow runs: {response.status_code}", file=sys.stderr)
        return {}

    runs = response.json().get("workflow_runs", [])
    
    # Group runs by (workflow_id, commit_sha)
    commit_history = {}
    for run in runs:
        sha = run.get("head_sha")
        workflow_id = run.get("workflow_id")
        key = (workflow_id, sha)
        
        if key not in commit_history:
            commit_history[key] = {
                "workflow_name": run.get("name"),
                "commit_sha": sha,
                "commit_message": run.get("head_commit", {}).get("message", "").split("\n")[0],
                "runs": []
            }
            
        commit_history[key]["runs"].append({
            "run_id": run.get("id"),
            "run_number": run.get("run_number"),
            "run_attempt": run.get("run_attempt", 1),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "html_url": run.get("html_url")
        })

    flaky_summary = []
    
    for (wf_id, sha), data in commit_history.items():
        conclusions = {r["conclusion"] for r in data["runs"] if r["conclusion"]}
        attempts = [r["run_attempt"] for r in data["runs"]]
        
        # A commit is FLAKY if it has BOTH success and failure, or retry attempts > 1
        has_pass_and_fail = ("success" in conclusions) and ("failure" in conclusions)
        has_retry = any(att > 1 for att in attempts)
        
        if has_pass_and_fail or has_retry:
            failed_run_ids = [r["run_id"] for r in data["runs"] if r["conclusion"] == "failure"]
            passed_run_ids = [r["run_id"] for r in data["runs"] if r["conclusion"] == "success"]
            
            flaky_summary.append({
                "workflow_name": data["workflow_name"],
                "commit_sha": sha[:7],
                "full_sha": sha,
                "commit_message": data["commit_message"],
                "flaky_reason": "Same commit has both PASS and FAIL runs" if has_pass_and_fail else "Retry attempt succeeded",
                "failed_runs": failed_run_ids,
                "passed_runs": passed_run_ids,
                "total_attempts": len(data["runs"])
            })

    result = {
        "repository": repo,
        "total_commits_evaluated": len(commit_history),
        "flaky_commits_count": len(flaky_summary),
        "flaky_commits": flaky_summary
    }

    print(f"\n=== FLAKY TEST DETECTION RESULT ===")
    print(f"Total Commits Evaluated: {result['total_commits_evaluated']}")
    print(f"Flaky Commits Identified: {result['flaky_commits_count']}\n")
    
    for flake in flaky_summary:
        print(f"[*] Commit: {flake['commit_sha']} - '{flake['commit_message']}'")
        print(f"    Reason: {flake['flaky_reason']}")
        print(f"    Failed Runs: {flake['failed_runs']}")
        print(f"    Passed Runs: {flake['passed_runs']}\n")


    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch GitHub Actions workflow logs & detect flaky runs.")
    parser.add_argument("--repo", required=True, help="Repository in format 'owner/repo'")
    parser.add_argument("--run-id", type=int, help="Specific workflow run ID to fetch")
    parser.add_argument("--token", help="GitHub Personal Access Token")
    parser.add_argument("--output-dir", default="logs", help="Directory to save extracted logs")
    parser.add_argument("--download-failed", action="store_true", help="Download logs for all failed runs")
    parser.add_argument("--detect-flaky", action="store_true", help="Run flake detection across repository commits")

    args = parser.parse_args()

    token = get_auth_token(args.token)
    if not token:
        print("[WARNING] No GitHub token found. Unauthenticated requests may encounter rate limits or permission errors.")

    if args.detect_flaky:
        flaky_data = detect_flaky_runs(args.repo, token)
        os.makedirs(args.output_dir, exist_ok=True)
        flaky_json_path = os.path.join(args.output_dir, "flaky_commits.json")
        with open(flaky_json_path, "w", encoding="utf-8") as f:
            json.dump(flaky_data, f, indent=2)
        print(f"Saved flaky commit analysis to {flaky_json_path}")

    if args.run_id:
        fetch_and_extract_run_logs(args.repo, args.run_id, args.output_dir, token)
    elif args.download_failed:
        failed_runs = list_failed_runs(args.repo, token)
        for run in failed_runs:
            fetch_and_extract_run_logs(args.repo, run["id"], args.output_dir, token)


if __name__ == "__main__":
    main()
