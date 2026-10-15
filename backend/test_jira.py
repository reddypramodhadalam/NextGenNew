"""
test_jira.py  —  standalone check of the Jira adapter (no UI, no generation).

Run it from the backend to confirm three things end to end:
  1. Credentials + connection work.
  2. The JQL returns your Features.
  3. Each issue's ADF description flattens to clean requirement text.

The output is exactly the BRD-equivalent string that will be fed to the
existing survey/generation. If this looks right, Stage 2 (wiring it into the
New Test Suite flow) is just plumbing.

Prerequisites — set these environment variables first:
    Windows PowerShell:
        $env:JIRA_BASE_URL   = "https://bodhiqinbox.atlassian.net"
        $env:JIRA_EMAIL      = "your-atlassian-email@example.com"
        $env:JIRA_API_TOKEN  = "your-api-token"
        # optional: $env:JIRA_PROJECT_KEY = "KAN"; $env:JIRA_ISSUE_TYPE = "Feature"

    Then, from the backend folder:
        python test_jira.py
"""

import sys
import traceback

try:
    from service.jira_adapter import (
        load_config, fetch_requirements, build_requirements_text,
    )
except ModuleNotFoundError:
    # Allow running the file from the same folder as jira_adapter.py too.
    from jira_adapter import (  # type: ignore
        load_config, fetch_requirements, build_requirements_text,
    )


def main() -> int:
    try:
        cfg = load_config()
    except RuntimeError as e:
        print("CONFIG ERROR:", e)
        return 2

    print(f"Connecting to {cfg.base_url}")
    print(f"JQL: {cfg.jql}\n")

    try:
        reqs = fetch_requirements(cfg)
    except Exception as e:  # network / auth / endpoint issues
        print("FETCH FAILED:", e)
        print("\nIf this is a 410/404 on /search/jql, your site may use the")
        print("classic endpoint — tell me and I'll switch the adapter to")
        print("/rest/api/3/search. If it's 401/403, re-check email + token.\n")
        traceback.print_exc()
        return 1

    if not reqs:
        print("No issues returned. Check the project key and issue type "
              "(is 'Feature' the right work type?), and that the two features "
              "exist in that project.")
        return 1

    print(f"Fetched {len(reqs)} issue(s):\n")
    for r in reqs:
        print(f"  {r.key}  [{r.issue_type} · {r.status}]  {r.summary}")
    print()

    print("=" * 70)
    print("BRD-EQUIVALENT REQUIREMENTS TEXT (this is what generation receives)")
    print("=" * 70)
    print(build_requirements_text(reqs))
    print("=" * 70)
    print("\nIf the text above reads like a clean BRD — requirement blocks with "
          "IDs, descriptions, and acceptance criteria — the adapter works and "
          "we can wire it into the survey (Stage 2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
