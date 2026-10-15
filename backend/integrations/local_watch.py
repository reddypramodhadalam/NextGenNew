"""
integrations/local_watch.py

Watches a local folder and automatically runs the tests whenever a
file in it changes - the "I change my code, tests just run" loop, with
nobody clicking anything. Pure local, no git host needed for this to work.

Everything is config-driven, no .py edits:
  - WATCH_FOLDER: which folder to watch, e.g. "local_repos/qa-automation-boutique"
  - WATCH_APP_NAME: which application these tests belong to, e.g. "BOUTIQUE"
  - WATCH_DEBOUNCE_SECONDS: how long to wait after the last change before
    running, so a burst of saves triggers one run, not ten (default 2)

Start it once in its own terminal:
    python -m integrations.local_watch
Leave it running. Edit files. It runs the tests on each change by itself.

This is a DEVELOPER CONVENIENCE loop for fast feedback while writing
scripts - it is separate from the audited run path. It calls the same
run_tests() everything else uses, so results are consistent.
"""

import logging
import os
import time

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from core.logging_config import configure_logging
from integrations.run_tests import run_tests

configure_logging()
logger = logging.getLogger(__name__)


class _ChangeHandler(FileSystemEventHandler):
    """
    Runs the tests when a watched file changes, with debouncing so a
    burst of saves triggers a single run.
    """

    def __init__(self, app_name: str, debounce_seconds: float):
        self.app_name = app_name
        self.debounce_seconds = debounce_seconds
        self._last_run = 0.0

    def on_modified(self, event):
        self._maybe_run(event)

    def on_created(self, event):
        self._maybe_run(event)

    def _maybe_run(self, event):
        if event.is_directory:
            return
        if not str(event.src_path).endswith((".java", ".py", ".ts")):
            return

        now = time.time()
        if now - self._last_run < self.debounce_seconds:
            return
        self._last_run = now

        logger.info("Change detected (%s) - running tests for %s", event.src_path, self.app_name)
        try:
            # correlation_id is a fixed label here since there's no HTTP
            # request behind a file-watcher run.
            result = run_tests(self.app_name, [], correlation_id="local-watch")
            logger.info("Watch run result: %s", result.get("overall_status"))
        except Exception:
            logger.exception("Watch run failed")


def main():
    """
    Starts watching the configured folder. Blocks until Ctrl+C.

    Raises:
        RuntimeError: if WATCH_FOLDER or WATCH_APP_NAME is not set.
    """
    watch_folder = os.environ.get("WATCH_FOLDER")
    app_name = os.environ.get("WATCH_APP_NAME")
    debounce = float(os.environ.get("WATCH_DEBOUNCE_SECONDS", "2"))

    if not watch_folder or not app_name:
        raise RuntimeError(
            "WATCH_FOLDER and WATCH_APP_NAME must both be set to use the local watcher."
        )

    logger.info("Watching '%s' for changes (app=%s, debounce=%ss)", watch_folder, app_name, debounce)

    handler = _ChangeHandler(app_name, debounce)
    observer = Observer()
    observer.schedule(handler, watch_folder, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("Watcher stopped")
    observer.join()


if __name__ == "__main__":
    main()
