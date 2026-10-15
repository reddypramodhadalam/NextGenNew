"""
core/logging_config.py

Configures logging for the whole application, once, at startup.

Replaces the previous setup (logging.basicConfig with a plain format
string) with a real JSON formatter. The difference matters: the old
format only wrapped the message itself in JSON - when an exception
was logged, Python's own default behaviour appends the traceback as
raw text on the lines after it, which breaks JSON parsing for exactly
the log lines someone would search for after a failure. This version
puts the traceback, file, and line number INSIDE the JSON object
itself, as real fields.
"""

import json
import logging
import sys

from core.middleware import get_correlation_id


class JSONFormatter(logging.Formatter):
    """
    Formats every log record as one JSON object per line.

    Includes the request's correlation id on every line, and, for any
    log call made with exc_info (logger.exception(), or
    logger.error(..., exc_info=True)), the full traceback plus the
    exact file and line number where it was logged from.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": get_correlation_id(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
            payload["file"] = record.pathname
            payload["line"] = record.lineno
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Sets up the root logger with the JSON formatter. Called once from
    api/main.py, before anything else logs a line.

    Args:
        level (int): minimum log level, defaults to INFO.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
