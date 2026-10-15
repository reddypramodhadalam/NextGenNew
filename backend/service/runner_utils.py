"""
service/runner_utils.py

Shared low-level utilities for survey, generation, and execution runners.

Responsibilities:
  - Browser (Chrome) driver factory.
  - pytest subprocess wrapper (_run_pytest).
  - Target URL resolution from application config.
  - Async progress helpers for worker-thread → event-loop communication.
  - Proposal tag writer.

Nothing here calls an LLM or touches SSO logic. Anything that needs those
imports auth_gateway or fast_generation directly.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from core.settings import settings

logger = logging.getLogger(__name__)

# ── Feature flags (read once at import, consistent across all runners) ─────────

FAST_GENERATION: bool = os.environ.get("FAST_GENERATION", "0") == "1"
VERIFY_GENERATED: bool = os.environ.get("VERIFY_GENERATED", "1") == "1"

# Maximum number of proposals generated concurrently. Each worker spins up
# its own Chrome driver and makes its own LLM call. Keep this ≤ the number
# of CPU cores you can spare for Chrome instances; 3 is a safe default on
# the 128 GB workstation.
PARALLEL_GENERATION_WORKERS: int = int(
    os.environ.get("PARALLEL_GENERATION_WORKERS", "3")
)

# Resolve generated tests from the project root rather than the process
# working directory, so local and server use the same location.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED_SCRIPTS_DIR = PROJECT_ROOT / "generated_tests"


# ── Browser factory ────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    """
    Create a configured Selenium Chrome driver.

    Reads SELENIUM_HEADED from settings. When True the browser window is
    visible (useful for debugging); False (default) runs headless.

    Kerberos / Windows Integrated Authentication:
    If KERBEROS_AUTH_DOMAINS is set (e.g. "*.baxter.com"), Chrome is
    configured to negotiate Kerberos tickets for matching domains — the
    same way your desktop browser silently authenticates without showing
    a login form.  Required for apps like Cognos Analytics that use
    Windows Integrated Authentication instead of an explicit login page.

    Raises:
        WebDriverException: If Chrome or ChromeDriver is not installed.
    """
    options = Options()
    if not settings.app.selenium_headed:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    _kerberos_domains = os.environ.get("KERBEROS_AUTH_DOMAINS", "").strip()
    if _kerberos_domains:
        options.add_argument(f"--auth-server-whitelist={_kerberos_domains}")
        options.add_argument(f"--auth-negotiate-delegate-whitelist={_kerberos_domains}")
        logger.debug("make_driver: Kerberos auth enabled for domains: %s", _kerberos_domains)
    from service.workflow_control import track_driver
    return track_driver(webdriver.Chrome(options=options))


# ── pytest wrapper ─────────────────────────────────────────────────────────────

def _run_owned_pytest(command, env, timeout):
    """Stop only this wizard's newly-created pytest process tree/group."""
    import signal
    from service.workflow_control import cancellation_requested, track_cleanup

    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, **kwargs)
    cleanup_error = None

    def stop():
        nonlocal cleanup_error
        if proc.poll() is not None:
            if cleanup_error:
                raise RuntimeError("Owned test-process cleanup could not be confirmed.") from cleanup_error
            return
        try:
            if os.name == "nt":
                killed = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=15,
                )
                if killed.returncode != 0:
                    raise RuntimeError("Could not confirm owned test-process tree termination.")
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=15)
            cleanup_error = None
        except Exception as exc:
            cleanup_error = exc
            raise

    track_cleanup(stop)
    started = time.monotonic()
    cancelled = False
    while True:
        if cancellation_requested() and proc.poll() is None:
            cancelled = True
            stop()
        if time.monotonic() - started > timeout and proc.poll() is None:
            stop()
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            stdout, stderr = proc.communicate(timeout=0.2)
            return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr), cancelled
        except subprocess.TimeoutExpired:
            continue


def run_pytest(
    script_path: str,
    headed: bool = False,
    target_url: Optional[str] = None,
    extra_env: Optional[dict] = None,
) -> dict:
    """
    Run a generated test script via pytest and capture the result.

    Args:
        script_path: Path to the .py test file.
        headed:      Show the browser while running.
        target_url:  Override APP_URL / NETRA_URL for this run.
        extra_env:   Additional environment variables (e.g. TEST_USERNAME).

    Returns:
        {
            "result":       "pass" | "fail" | "error"
            "output":       Combined stdout + stderr
            "duration_ms":  Run duration in milliseconds
            "run_id":       Identifier used for the screenshot folder
            "screenshot_dir": Path to this run's screenshot folder
        }
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    env = os.environ.copy()

    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env["HEADED"] = "1" if headed else "0"
    env["RUN_ID"] = run_id
    env.setdefault("SCREENSHOT_DIR", "screenshots")

    # Generated scripts import NextGen2_evidence; keep both canonical helper
    # locations on PYTHONPATH regardless of working directory.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [
            str(Path(__file__).parent.parent / "generated_tests"),
            str(Path(__file__).resolve().parents[2] / "generated_tests"),
            env.get("PYTHONPATH", ""),
        ] if p
    )

    if headed:
        env.setdefault("STEP_PAUSE", "1.2")

    if target_url:
        env["APP_URL"] = target_url
        env["NETRA_URL"] = target_url   # backwards compat for older scripts

    if extra_env:
        env.update(extra_env)

    start = time.time()
    try:
        from service.workflow_control import current_operation
        cancelled = False
        if current_operation():
            proc, cancelled = _run_owned_pytest(
                ["pytest", script_path, "-v", "--tb=short"], env,
                300 if headed else 120,
            )
        else:
            proc = subprocess.run(
                ["pytest", script_path, "-v", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=300 if headed else 120,
                env=env,
            )
        duration_ms = int((time.time() - start) * 1000)
        result = "cancelled" if cancelled else ("pass" if proc.returncode == 0 else "fail")
        output = proc.stdout + proc.stderr
        if cancelled:
            output += "\nCancelled by user. Partial execution; no completed test verdict."
        logger.info("pytest finished: script=%s result=%s duration_ms=%d",
                    script_path, result, duration_ms)
    except subprocess.TimeoutExpired:
        duration_ms = 120_000
        result = "error"
        output = "Test execution timed out after 120 seconds."
        logger.error("pytest timed out: script=%s", script_path)
    except Exception as e:
        duration_ms = 0
        result = "error"
        output = str(e)
        logger.exception("pytest failed to run: script=%s", script_path)

    return {
        "result": result,
        "output": output,
        "duration_ms": duration_ms,
        "run_id": run_id,
        "screenshot_dir": str(Path(env["SCREENSHOT_DIR"]) / run_id),
    }


# ── URL resolution ─────────────────────────────────────────────────────────────

def resolve_target_url(application: dict, environment_key: str) -> str:
    """
    Resolve the target URL for a given application and environment.

    The application's own base_url is the single source of truth.
    config/applications.yaml may supply ADDITIONAL named environments
    (uat, stage), but it never overrides base_url on a name match.

    Args:
        application:     The application row dict from the database.
        environment_key: The environment name, e.g. 'default', 'uat'.

    Returns:
        The resolved URL string. Empty only if the application has neither
        base_url nor matching config.
    """
    from service.context_resolver import resolve

    base_url = (application.get("base_url") or "").strip()

    def _from_config(env: str) -> Optional[str]:
        app_configs = settings.applications_config.get("applications", {})
        app_key = next(
            (k for k, v in app_configs.items()
             if v.get("display_name") == application["name"]),
            None,
        )
        if not app_key:
            return None
        try:
            return resolve(app_key, env).url
        except Exception as e:
            logger.warning("No config for app=%s env=%s: %s", app_key, env, e)
            return None

    if environment_key and environment_key != "default":
        url = _from_config(environment_key)
        if url:
            logger.info("Resolved '%s' env=%s from config: %s",
                        application["name"], environment_key, url)
            return url
        logger.warning(
            "Environment '%s' not configured for '%s' — using base_url %s",
            environment_key, application["name"], base_url,
        )

    if base_url:
        return base_url

    url = _from_config(environment_key or "default")
    if url:
        logger.warning("Application '%s' has no base_url — using config url %s",
                       application["name"], url)
        return url

    logger.warning("Application '%s' has neither base_url nor usable config",
                   application["name"])
    return ""


# ── Progress helpers ───────────────────────────────────────────────────────────

def sync_progress_status(loop, progress_callback: Callable, message: str) -> None:
    """
    Emit a status message from synchronous/worker-thread code.

    Survey runs in a worker thread via asyncio.to_thread. This hands the
    coroutine back to the event loop thread-safely so progress events
    reach the browser during the survey, not all at once at the end.
    """
    try:
        asyncio.run_coroutine_threadsafe(
            progress_callback("status", {"message": message}), loop
        )
    except Exception as e:
        logger.debug("Could not emit progress %r: %s", message[:40], e)


def sync_progress(progress_callback: Callable, step_num: int, step: dict) -> None:
    """
    Synchronous wrapper for the async progress callback.

    Agent loops (run_explorer, run_author) are synchronous. This lets them
    fire progress events into the async SSE queue.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                progress_callback("agent_step", {
                    "step_num": step_num,
                    "action": step.get("action"),
                    "message": f"Step {step_num}: {step.get('action', '')}",
                })
            )
    except Exception:
        pass   # progress display is non-critical


# ── Proposal helpers ───────────────────────────────────────────────────────────

def set_proposal_tags(proposal_id: int, tags: dict) -> None:
    """
    Write the scenario group and requirement link onto a proposal.

    Kept here rather than widening proposals_db.create so the existing
    query layer is untouched.
    """
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE test_case_proposals SET tags = %s, updated_at = now() WHERE id = %s",
                (json.dumps(tags), proposal_id),
            )
        conn.commit()
