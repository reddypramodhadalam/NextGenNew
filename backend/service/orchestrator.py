"""
service/orchestrator.py  — thin coordinator shim

All logic has moved to focused modules:

  - survey_runner.py      run_new_test_suite()
  - generation_runner.py  run_generate_scripts()
  - execution_runner.py   run_execute_scripts()
  - runner_utils.py       shared utilities + constants
  - auth_gateway.py       SSO / authentication precheck
  - document_extractor.py document text extraction

This module re-exports the three public functions so that every
caller in api/routes/ keeps working with zero changes.
"""

from service.survey_runner import run_new_test_suite
from service.generation_runner import run_generate_scripts
from service.execution_runner import run_execute_scripts
from service.runner_utils import FAST_GENERATION, VERIFY_GENERATED, GENERATED_SCRIPTS_DIR

__all__ = [
    "run_new_test_suite",
    "run_generate_scripts",
    "run_execute_scripts",
    "FAST_GENERATION",
    "VERIFY_GENERATED",
    "GENERATED_SCRIPTS_DIR",
]
