"""
service/selenium_runner.py

Runs generated pytest+Selenium scripts and records results + audit
evidence (screenshots per run).

For audit (CSV/GxP): every run gets a unique RUN_ID and its own
screenshots/<RUN_ID>/ folder. The paths are returned so they can be
stored with the execution record and shown in the UI.

HEADED=1 opens Chrome visibly and (via the scripts) runs slowly enough
to watch. STEP_PAUSE controls the per-step delay.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCREENSHOT_ROOT = os.environ.get("SCREENSHOT_DIR", "screenshots")


def run_script_file(
    script_path: str,
    headed: bool = False,
    target_url: str | None = None,
    extra_env: dict | None = None,
) -> dict:
    """
    Run one Selenium script with pytest; capture result + screenshot dir.

    Returns dict: result, output, duration_ms, run_id, screenshot_dir,
    screenshots (list of file paths).
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    shot_dir = Path(SCREENSHOT_ROOT) / run_id

    env = os.environ.copy()
    # Keep test data run-scoped: inherited TEST_* values from the parent
    # process can leak stale credentials into unrelated scripts.
    for k in list(env.keys()):
        if k.startswith("TEST_"):
            env.pop(k, None)

    # Isolate runs from globally-installed pytest plugins (for example,
    # pytest-bdd) that are unrelated to generated Selenium scripts and can
    # crash collection with import/version mismatches.
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env["HEADED"] = "1" if headed else "0"
    env["RUN_ID"] = run_id
    env["SCREENSHOT_DIR"] = SCREENSHOT_ROOT
    # Generated scripts import NextGen2_evidence; keep both possible module
    # locations on PYTHONPATH regardless of current working directory.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [
            str(Path(__file__).parent.parent / "generated_tests"),
            str(Path(__file__).resolve().parents[2] / "generated_tests"),
            env.get("PYTHONPATH", ""),
        ] if p
    )
    if headed:
        env.setdefault("STEP_PAUSE", "1.5")   # watchable in demo
    if target_url:
        # APP_URL is what newly generated scripts read. NETRA_URL is kept so
        # scripts generated before the environment change keep working.
        env["APP_URL"] = target_url
        env["NETRA_URL"] = target_url
    if extra_env:
        env.update({str(k): "" if v is None else str(v) for k, v in extra_env.items()})

    start = time.time()
    try:
        proc = subprocess.run(
            ["pytest", script_path, "-v", "--tb=short", "--no-header", "-s"],
            capture_output=True, text=True, timeout=300, env=env,
        )
        duration_ms = int((time.time() - start) * 1000)
        result = "pass" if proc.returncode == 0 else "fail"
        output = (proc.stdout + proc.stderr)[-5000:]
    except subprocess.TimeoutExpired:
        return {"result": "error", "output": "Timed out after 300s", "duration_ms": 300000,
                "run_id": run_id, "screenshot_dir": str(shot_dir), "screenshots": []}
    except FileNotFoundError:
        return {"result": "error", "output": "pytest not found. pip install pytest selenium",
                "duration_ms": 0, "run_id": run_id, "screenshot_dir": str(shot_dir), "screenshots": []}

    # collect screenshot files captured this run (audit evidence)
    screenshots = []
    if shot_dir.exists():
        screenshots = sorted(str(p) for p in shot_dir.glob("*.png"))

    return {
        "result": result,
        "output": output,
        "duration_ms": duration_ms,
        "run_id": run_id,
        "screenshot_dir": str(shot_dir),
        "screenshots": screenshots,
        "step_results": _read_step_ledger(shot_dir),
    }


def _read_step_ledger(shot_dir: Path) -> list | None:
    """
    Read the per-step ledger a generated script wrote during the run.

    Written by generated_tests/kavach_evidence.py, one entry per numbered
    step. Returns None when the file is absent, which is the normal case for
    a script generated before step recording existed — those runs keep
    working and simply carry no step detail.

    Args:
        shot_dir: This run's screenshot folder, where the ledger lives.

    Returns:
        The list of step records, or None.
    """
    ledger = shot_dir / "steps.json"
    if not ledger.exists():
        return None
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except (OSError, json.JSONDecodeError) as e:
        # Evidence that cannot be read is worth a log line, but never worth
        # failing a run that has already completed.
        logger.warning("Could not read step ledger %s: %s", ledger, e)
        return None
