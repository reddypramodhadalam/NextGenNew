"""
api/routes/chat_actions.py

Letting the assistant act, with a confirmation in between.

  POST /v1/chat/interpret    instruction -> proposal. Executes nothing.
  POST /v1/chat/resummarise  rebuild the sentence after the user edits a field
  POST /v1/chat/execute      carry out a confirmed proposal

Three endpoints rather than one with flags, so executing by accident is not
possible: interpreting cannot change anything, and executing accepts only a
proposal that was already produced and shown.

The question endpoint in chat.py is untouched, so asking something can never
mutate state.
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from service import actions

logger = logging.getLogger(__name__)
router = APIRouter()

# A wide filter, not a decision. It exists only so plainly conversational
# messages do not pay for a model call; anything that might be an
# instruction is passed on and the MODEL decides — it can reply that this is
# a question. A false positive costs one call; a false negative costs the
# feature, so the net is loose on purpose.
ACTION_HINTS = (
    "run", "execute", "schedule", "scheduled", "scheduling", "pause", "paused",
    "resume", "stop", "start", "create", "set up", "setup", "kick off",
    "kickoff", "trigger", "disable", "enable", "fire off", "launch",
    "every day", "every weekday", "every morning", "every week", "daily",
    "nightly", "weekly", "hourly", "each morning", "tonight", "tomorrow",
    # Raising a ticket. Without these, "can you raise a ticket for me" fails
    # this gate and is answered as a question — the request never reaches the
    # interpreter at all. Every word added here must also be recognised by
    # service/actions/runner.py::_prefilter, or a message passes this gate
    # only to be judged conversational one layer down.
    "ticket", "raise", "escalate", "file a", "log a", "report a", "open a",
)

QUESTION_OPENERS = (
    "how ", "what ", "which ", "why ", "when ", "who ", "is ", "are ",
    "do we", "does ", "show me", "tell me", "can i see", "list ",
)


def might_be_instruction(text: str) -> bool:
    """Whether a message is worth showing to the interpreter as an action.

    A filter, not a decision: anything that could plausibly be an instruction
    is passed on, and the interpreter decides. A false positive costs one
    model call; a false negative costs the feature silently, which is far
    worse and much harder to notice — the user simply gets an answer instead
    of the thing they asked for, with nothing logged to say why.

    Args:
        text: What the user typed.

    Returns:
        True if it should be interpreted.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith(QUESTION_OPENERS):
        return False
    return any(w in t for w in ACTION_HINTS)


class InterpretRequest(BaseModel):
    instruction: str


class ResummariseRequest(BaseModel):
    proposal: dict


class ExecuteRequest(BaseModel):
    proposal: dict


@router.get("/chat/actions")
def list_actions():
    """
    Everything the assistant is allowed to do.

    Exposed deliberately: in a validated environment "what can an LLM
    trigger?" needs an answer you can read, and this is it.
    """
    return {
        "actions": [
            {"name": n, "description": a.description,
             "parameters": a.parameters, "editable": list(a.confirm_fields)}
            for n, a in actions.ACTIONS.items()
        ]
    }


@router.post("/chat/interpret")
def interpret(body: InterpretRequest):
    """Turn an instruction into a proposal. Nothing is executed."""
    text = (body.instruction or "").strip()
    if not text:
        return {"error": "Nothing to interpret."}
    if not might_be_instruction(text):
        return {"not_an_action": True}

    try:
        from llm.provider_factory import get_provider
        provider = get_provider()
    except Exception as e:
        logger.warning("No model available to interpret instructions: %s", e)
        return {"error": "The model is not available."}

    try:
        return actions.interpret(text, provider)
    except Exception as e:
        logger.exception("Interpretation failed")
        return {"error": str(e)}


@router.post("/chat/resummarise")
def resummarise(body: ResummariseRequest):
    """Rebuild the confirmation sentence after an edit, so it stays truthful."""
    return {"summary": actions.resummarise(body.proposal or {})}


@router.post("/chat/execute")
def execute(body: ExecuteRequest, request: Request):
    """Carry out a proposal the user has confirmed."""
    who = request.headers.get("X-Triggered-By", "chat")
    if not body.proposal or not body.proposal.get("action"):
        return {"error": "No action to carry out."}
    try:
        return actions.execute(body.proposal, triggered_by=who)
    except Exception as e:
        logger.exception("Action failed")
        return {"error": str(e)}