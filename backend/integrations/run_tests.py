"""
integrations/run_tests.py

The single function that runs tests. Called from api/routes/execution.py,
which is called both by the UI's Execute button and by a check-in
trigger - both hit the same route, this function never knows or cares
which one called it.

EXECUTION_MODE (an environment variable) decides WHERE tests run:
  - "local"     -> runs on this machine, using Maven directly
  - "bitbucket" -> tells Bitbucket Pipelines to run it
  - "jenkins"   -> tells Jenkins to run it

The correlation_id is NOT generated in this file. It comes from
core/middleware.py, generated once when the HTTP request arrived, and
is passed in here so every log line this function writes ties back to
that same request - matching every other part of the application,
rather than inventing a second, disconnected id scheme just for test runs.

When multiple test files are selected, local mode runs them together
in one Maven command, then reads Maven's own XML output afterward to
report a real pass or fail per individual test file - not one blended
result for the whole batch.
"""

import logging
import os
import subprocess
import xml.etree.ElementTree as ET

import requests

from core.errors import (
    MavenExecutionError,
    MavenNotFoundError,
    TestFileNotFoundError,
    TriggerFailedError,
    UnknownExecutionModeError,
)
from integrations.app_repo_map import get_repo_slug
from integrations.git_provider import get_git_provider

logger = logging.getLogger(__name__)

EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "local")


def run_tests(app_name: str, test_file_names: list[str], correlation_id: str) -> dict:
    """
    Runs tests for an application, using whichever execution mode is configured.

    Args:
        app_name (str): which application to run tests for, e.g. "FS".
        test_file_names (list[str]): specific test files to run. Empty
            list runs every test in that application's repository.
        correlation_id (str): the current request's correlation id,
            from core/middleware.py - reused here, not regenerated.

    Returns:
        dict: always contains "mode", "app_name", "overall_status".
            Local mode additionally includes "per_test_results".

    Raises:
        ApplicationNotMappedError: the application has no repository mapped.
        RepositoryNotFoundError: the repository does not exist yet.
        RepositorySyncError: the repository could not be reached or pulled.
        TestFileNotFoundError: a requested test file was not found.
        MavenNotFoundError: Maven is not installed (local mode).
        MavenExecutionError: Maven did not finish normally (local mode).
        TriggerFailedError: Bitbucket or Jenkins rejected the trigger.
        UnknownExecutionModeError: EXECUTION_MODE is unsupported.
    """
    logger.info(
        "Run requested: app=%s files=%s mode=%s", app_name, test_file_names, EXECUTION_MODE,
    )

    if EXECUTION_MODE == "local":
        return _run_local(app_name, test_file_names)
    if EXECUTION_MODE == "bitbucket":
        return _run_via_bitbucket(app_name, test_file_names)
    if EXECUTION_MODE == "jenkins":
        return _run_via_jenkins(app_name, test_file_names)

    logger.error("Unknown EXECUTION_MODE: %s", EXECUTION_MODE)
    raise UnknownExecutionModeError(
        f"Unknown EXECUTION_MODE '{EXECUTION_MODE}'. Set it to one of: local, bitbucket, jenkins"
    )


def _find_missing_test_files(local_path: str, test_file_names: list[str]) -> list[str]:
    """Confirms every requested test file exists before anything is run."""
    search_root = os.path.join(local_path, "src", "test", "java")
    all_files = set()
    for _root_dir, _dirs, files in os.walk(search_root):
        all_files.update(files)
    return [name for name in test_file_names if name not in all_files]


def _parse_surefire_reports(reports_dir: str) -> list[dict]:
    """Reads Maven Surefire's XML output into one result per test class."""
    results = []
    if not os.path.isdir(reports_dir):
        return results

    for filename in sorted(os.listdir(reports_dir)):
        if not filename.endswith(".xml"):
            continue
        path = os.path.join(reports_dir, filename)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            logger.warning("Unreadable surefire report: %s", path)
            continue

        failures = int(root.get("failures", 0))
        errors = int(root.get("errors", 0))
        status = "PASS" if (failures == 0 and errors == 0) else "FAIL"

        message = None
        if status == "FAIL":
            failure_node = root.find(".//failure") or root.find(".//error")
            if failure_node is not None:
                message = failure_node.get("message")

        results.append({
            "class_name": root.get("name", filename),
            "tests_run": int(root.get("tests", 0)),
            "failures": failures,
            "errors": errors,
            "status": status,
            "message": message,
        })

    return results


def _run_local(app_name: str, test_file_names: list[str]) -> dict:
    """Runs the tests on this machine with Maven, per-test results via Surefire XML."""
    repo_slug = get_repo_slug(app_name)
    local_path = get_git_provider().sync_repo_locally(repo_slug)

    missing = _find_missing_test_files(local_path, test_file_names)
    if missing:
        logger.error("Test files not found for '%s': %s", app_name, missing)
        raise TestFileNotFoundError(f"These test files were not found in '{repo_slug}': {missing}")

    command = ["mvn", "test"]
    if test_file_names:
        test_classes = ",".join(name.replace(".java", "") for name in test_file_names)
        command.append(f"-Dtest={test_classes}")

    try:
        result = subprocess.run(command, cwd=local_path, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError as e:
        logger.exception("Maven not found on this machine")
        raise MavenNotFoundError("Maven ('mvn') is not installed, or not on PATH, on this machine") from e
    except subprocess.TimeoutExpired as e:
        logger.exception("Maven run timed out for '%s'", app_name)
        raise MavenExecutionError("Maven run exceeded the 30 minute time limit and was stopped") from e

    per_test_results = _parse_surefire_reports(os.path.join(local_path, "target", "surefire-reports"))
    overall_status = "PASS" if result.returncode == 0 else "FAIL"

    logger.info("Local run finished: app=%s status=%s results=%s", app_name, overall_status, per_test_results)

    return {
        "mode": "local",
        "app_name": app_name,
        "overall_status": overall_status,
        "per_test_results": per_test_results,
        "raw_output": result.stdout + result.stderr,
    }


def _run_via_bitbucket(app_name: str, test_file_names: list[str]) -> dict:
    """Triggers a Bitbucket Pipelines run; does not wait for it to finish."""
    repo_slug = get_repo_slug(app_name)
    username = os.environ["BITBUCKET_USERNAME"]
    app_password = os.environ["BITBUCKET_APP_PASSWORD"]

    url = f"https://api.bitbucket.org/2.0/repositories/baxter/{repo_slug}/pipelines/"
    payload = {"target": {"ref_type": "branch", "type": "pipeline_ref_target", "ref_name": "main"}}
    if test_file_names:
        payload["variables"] = [{"key": "TEST_FILES", "value": ",".join(test_file_names)}]

    try:
        response = requests.post(url, auth=(username, app_password), json=payload, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.exception("Bitbucket trigger failed for '%s'", app_name)
        raise TriggerFailedError(f"Could not trigger Bitbucket Pipelines for '{repo_slug}'") from e

    pipeline_uuid = response.json().get("uuid")
    logger.info("Bitbucket pipeline triggered: app=%s pipeline_uuid=%s", app_name, pipeline_uuid)

    return {"mode": "bitbucket", "app_name": app_name, "overall_status": "TRIGGERED", "pipeline_uuid": pipeline_uuid}


def _run_via_jenkins(app_name: str, test_file_names: list[str]) -> dict:
    """Triggers a Jenkins build; does not wait for it to finish."""
    jenkins_url = os.environ.get("JENKINS_URL", "http://localhost:8080")
    jenkins_user = os.environ["JENKINS_USERNAME"]
    jenkins_token = os.environ["JENKINS_API_TOKEN"]

    url = f"{jenkins_url}/job/autoqa-run/buildWithParameters"
    params = {"APP_FOLDER": app_name}
    if test_file_names:
        params["TEST_FILES"] = ",".join(test_file_names)

    try:
        response = requests.post(url, auth=(jenkins_user, jenkins_token), params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.exception("Jenkins trigger failed for '%s'", app_name)
        raise TriggerFailedError("Could not trigger the Jenkins job 'autoqa-run'") from e

    logger.info("Jenkins build triggered: app=%s", app_name)
    return {"mode": "jenkins", "app_name": app_name, "overall_status": "TRIGGERED"}
