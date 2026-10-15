"""
service/actions

Actions the assistant can perform on the user's behalf.

  domain.py    values shared with the database schema
  base.py      what an Action is
  matching.py  resolving model-returned names against real records
  catalog.py   the actions themselves — one declaration each
  runner.py    interpret, validate, resolve, execute

Adding an action means adding one Action to catalog.py. The tool schema,
validation, confirmation wording and execution all derive from it.
"""

from service.actions.runner import (          # noqa: F401
    interpret, execute, resummarise, tool_definitions, catalogue,
)
from service.actions.catalog import ACTIONS   # noqa: F401
