"""
run_selenium.py

Executes generated pytest+Selenium scripts and records results.

Supports:
  - running ONE script, or ALL scripts under a scenario
  - HEADED or headless (headed = you watch Chrome; used in the demo)
  - real pass/fail from pytest's exit code + output

Called by the execution API route. Nothing here is LLM-dependent — the
scripts already exist on disk; this just runs them with pytest, which
drives Chrome via Selenium.
"""

import os
import subprocess
import time
from pathlib import Path

GENERATED_DIR = Path(os.environ.get("GENERATED_TESTS_DIR", "generated_tests"))


def run_script(script_path: str, headed: bool = False, target_url: str | None = None) -> dict:
    """
    Run a single Selenium test script with pytest.

    Args:
        script_path: path to the .py test file.
        headed: if True, Chrome opens visibly (demo mode).
        target_url: overrides the site under test (NETRA_URL env).

    Returns:
        dict: result ('pass'|'fail'|'error'), output, duration_ms.
    """
    env = os.environ.copy()
    env["HEADED"] = "1" if headed else "0"
    if target_url:
        env["NETRA_URL"] = target_url

    start = time.time()
    try:
        proc = subprocess.run(
            ["pytest", script_path, "-v", "--tb=short", "--no-header"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        duration_ms = int((time.time() - start) * 1000)
        result = "pass" if proc.returncode == 0 else "fail"
        return {
            "result": result,
            "output": (proc.stdout + proc.stderr)[-4000:],
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {"result": "error", "output": "Test timed out after 180s", "duration_ms": 180000}
    except FileNotFoundError:
        return {"result": "error", "output": "pytest not found. pip install pytest selenium", "duration_ms": 0}


def run_scenario(script_paths: list[str], headed: bool = False, target_url: str | None = None) -> list[dict]:
    """Run every script under a scenario; return one result per script."""
    return [
        {"script_path": p, **run_script(p, headed=headed, target_url=target_url)}
        for p in script_paths
    ]
