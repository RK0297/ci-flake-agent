"""
src/fetch_logs.py - Fetch GitHub Actions workflow logs via GitHub REST API.
"""

import os
import sys
import argparse
import requests


def fetch_workflow_run_logs(repo: str, run_id: int, token: str = None) -> str:
    """
    Fetch raw logs for a specified GitHub Actions workflow run ID.
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
    }
    
    auth_token = token or os.getenv("GITHUB_TOKEN")
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"
    print(f"Fetching logs from: {url}")
    
    response = requests.get(url, headers=headers, allow_redirects=True)
    if response.status_code == 200:
        return response.text
    elif response.status_code == 404:
        print(f"Error: Workflow run {run_id} or repository '{repo}' not found.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Error: API returned status code {response.status_code}: {response.reason}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Fetch GitHub Actions workflow logs.")
    parser.add_argument("--repo", required=True, help="Repository in format 'owner/repo'")
    parser.add_argument("--run-id", type=int, required=True, help="Workflow run ID")
    parser.add_argument("--token", help="GitHub Personal Access Token")
    parser.add_argument("--output", default="logs.txt", help="Path to output file for fetched logs")

    args = parser.parse_args()

    logs = fetch_workflow_run_logs(args.repo, args.run_id, args.token)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(logs)

    print(f"Successfully saved logs to {args.output}")


if __name__ == "__main__":
    main()
