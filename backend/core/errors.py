"""
core/errors.py

Every error the application raises on purpose, as named, typed
classes - never generic ValueError/RuntimeError for anything a caller
should be able to react to specifically.

Each class carries:
  - code:        a short, stable, machine-readable string
  - status_code: the HTTP status api/main.py's central handler returns

This is what replaces "everything is INTERNAL_ERROR" with an actual
answer to "what exactly failed". A response now looks like:

    {"error": {"code": "REPOSITORY_NOT_FOUND",
               "message": "Repository 'qa-automation-fs' does not exist yet.",
               "correlation_id": "a1b2c3..."}}

and the full traceback, with file and line number, is written to the
server-side log under that same correlation_id - never sent to the caller.
"""


class AppError(Exception):
    """
    Base class for every error this application raises on purpose.

    Args:
        message (str): human-readable detail. Safe to return to the
            caller - never put anything sensitive in this string.
    """
    code = "APP_ERROR"
    status_code = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ApplicationNotMappedError(AppError):
    """The application name given has no repository mapped to it yet."""
    code = "APPLICATION_NOT_MAPPED"
    status_code = 400


class RepositoryNotFoundError(AppError):
    """The Bitbucket repository for this application does not exist yet."""
    code = "REPOSITORY_NOT_FOUND"
    status_code = 404


class RepositorySyncError(AppError):
    """Reaching, cloning, or pulling the repository failed."""
    code = "REPOSITORY_SYNC_FAILED"
    status_code = 502


class TestFileNotFoundError(AppError):
    """A specifically requested test file does not exist in the repository."""
    code = "TEST_FILE_NOT_FOUND"
    status_code = 404


class MavenNotFoundError(AppError):
    """The 'mvn' command is not installed, or not on PATH, on this machine."""
    code = "MAVEN_NOT_FOUND"
    status_code = 500


class MavenExecutionError(AppError):
    """Maven started but did not finish normally, e.g. it timed out."""
    code = "MAVEN_EXECUTION_FAILED"
    status_code = 500


class TriggerFailedError(AppError):
    """Triggering a run on Bitbucket Pipelines or Jenkins failed."""
    code = "TRIGGER_FAILED"
    status_code = 502


class UnknownExecutionModeError(AppError):
    """EXECUTION_MODE is set to a value this application does not support."""
    code = "UNKNOWN_EXECUTION_MODE"
    status_code = 500


class LLMConfigurationError(AppError):
    """The configured LLM provider is missing required settings."""
    code = "LLM_CONFIGURATION_ERROR"
    status_code = 500
