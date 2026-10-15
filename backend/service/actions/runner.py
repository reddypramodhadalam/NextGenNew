"""
service/actions/runner.py

Getting from an instruction to a confirmed action.

    instruction
        │
        ├─ catalogue()      what exists right now: applications, suites, schedules
        │
        ├─ model            given the tool schemas built from the catalog.
        │                   Native tool calling where the model supports it,
        │                   JSON otherwise — the caller does not need to know
        │                   which happened.
        │
        ├─ validate         arguments checked against the action's own JSON
        │                   Schema, so a malformed call is refused before it
        │                   reaches any resolver
        │
        ├─ resolve          names to ids, times to timestamps. Ambiguity
        │                   becomes a question, never a guess.
        │
        └─ Resolution       returned for confirmation. NOTHING has executed.

execute() is separate and takes only a resolved action, so nothing can run
without having been shown first.
"""

import json
import logging
import re
from typing import Optional

from core.db.connection import get_conn
from service.actions.base import Resolution
from service.actions.catalog import ACTIONS, SUMMARISERS
from service.actions.timeparse import parse_time, parse_day

logger = logging.getLogger(__name__)


def catalogue() -> dict:
    """
    What currently exists, for the model to choose from.

    Given real names, the model picks one; given none, it invents something
    plausible that then fails to resolve. Cheap to fetch and worth it.
    """
    out = {"applications": [], "suites": [], "schedules": []}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, base_url FROM applications "
                        "WHERE is_archived = FALSE ORDER BY name")
            out["applications"] = [{"id": r[0], "name": r[1], "url": r[2]}
                                   for r in cur.fetchall()]

            cur.execute("""SELECT s.id, s.name, s.kind, a.name, a.id
                           FROM test_suites s JOIN applications a ON a.id = s.application_id
                           ORDER BY a.name, s.name""")
            out["suites"] = [{"id": r[0], "name": r[1], "kind": r[2],
                              "application": r[3], "application_id": r[4]}
                             for r in cur.fetchall()]

            cur.execute("""SELECT s.id, s.name, s.enabled, a.name
                           FROM schedules s JOIN applications a ON a.id = s.application_id
                           ORDER BY s.name""")
            out["schedules"] = [{"id": r[0], "name": r[1], "enabled": r[2],
                                 "application": r[3]}
                                for r in cur.fetchall()]
    return out


def tool_definitions() -> list[dict]:
    """Tool schemas for every action, built from the catalog declarations."""
    return [a.tool_schema() for a in ACTIONS.values()]


def validate(action_name: str, args: dict) -> Optional[str]:
    """
    Check arguments against the action's own JSON Schema.

    The same schema the model was given, so the contract cannot drift
    between what was asked for and what is accepted. Deliberately minimal —
    required fields, declared enums, types — rather than a full validator,
    because the resolver does the meaningful checking against real data.

    Returns:
        An error message, or None when the arguments are acceptable.
    """
    action = ACTIONS.get(action_name)
    if not action:
        return f"'{action_name}' is not an action I can perform."

    schema = action.parameters
    props = schema.get("properties", {})

    for required in schema.get("required", []):
        if not args.get(required):
            return f"'{action_name}' needs {required}, which was not given."

    for key, value in list(args.items()):
        if key not in props:
            args.pop(key, None)          # ignore anything not declared
            continue
        spec = props[key]
        if value is None:
            continue
        if spec.get("type") == "string" and not isinstance(value, str):
            args[key] = str(value)
            value = args[key]
        allowed = spec.get("enum")
        if allowed and value and value not in allowed:
            return (f"'{value}' is not a valid {key}. "
                    f"Expected one of: {', '.join(allowed)}.")
    return None


# ── deterministic pre-filter ─────────────────────────────────────────────
#
# Two things are decided in code before the model is asked anything.
#
# Raising a ticket, because it must not depend on a 14B model choosing a tool.
# "can you raise a ticket for me, I can't work out how to do X" asks for two
# things at once, and a small model reads it as a question and answers instead
# — which is exactly the moment a user who asked for help gets none. An
# explicit request for a ticket is unambiguous enough to recognise with a
# pattern, so it is.
#
# Plain questions, because interpreting them costs a full model call that can
# only ever come back "not an action". Skipping it halves the latency of every
# question asked in the panel, which on a local model is the single largest
# delay a user feels.

_TICKET_REQUEST = re.compile(
    r"\b("
    r"raise (?:a |an )?(?:jira |support |new )*(?:ticket|issue|request)"
    r"|create (?:a |an )?(?:jira |support )*(?:ticket|issue)"
    r"|(?:file|log|open|submit) (?:a |an )?(?:jira |support )*(?:ticket|issue|bug)"
    r"|report (?:this|it) (?:as )?(?:a )?(?:bug|issue|problem)"
    r"|escalate this"
    r")\b",
    re.IGNORECASE,
)

# Phrases that precede the real content of a ticket request and should not end
# up in its summary. Stripped so "can you raise a ticket for me, I can't create
# a regression suite" files as "I can't create a regression suite" rather than
# repeating the request back.
_TICKET_PREAMBLE = re.compile(
    r"^\s*(?:hi|hey|please|pls|kindly|can you|can u|could you|would you|"
    r"i want you to|i need you to|i'd like you to)?[\s,]*"
    r"(?:please |pls )?"
    r"(?:raise|create|file|log|open|submit)\s+(?:a |an )?"
    r"(?:jira |support |new )*(?:ticket|issue|request|bug)"
    r"(?:\s+(?:in|on|with|via)\s+\w+)?"
    r"(?:\s+for\s+me)?"
    r"[\s,.:;-]*",
    re.IGNORECASE,
)

# A question about the product, not an instruction to change anything.
#
# Checked BEFORE the ticket pattern, and that order is the whole point:
# "how do I raise a ticket" is someone asking to be taught, and filing a
# ticket in reply would be both wrong and faintly absurd. An interrogative
# opener beats a verb appearing later in the sentence.
_PLAIN_QUESTION = re.compile(
    r"^\s*(?:"
    r"how (?:do|can|would|should|might|does) (?:i|we|you|it)"
    r"|how to\b"
    r"|what (?:is|are|does|do|happens)"
    r"|where (?:is|are|do|can)"
    r"|why (?:do|does|is|are)"
    r"|when (?:do|does|should)"
    r"|how (?:many|much|often)"
    r"|which\b"
    r"|who\b"
    r"|show me\b"
    r"|list (?:the|all|my)"
    r"|tell me (?:about|how|what)"
    r"|explain\b"
    r")",
    re.IGNORECASE,
)


def _prefilter(instruction: str) -> Optional[tuple[Optional[str], dict]]:
    """Decide an instruction without asking the model, where that is safe.

    Args:
        instruction: What the user typed.

    Returns:
        (action_name, args) to act on, (None, {}) to treat it as a question,
        or None when the model should decide after all.
    """
    text = (instruction or "").strip()
    if not text:
        return None

    # Questions first. "How do I raise a ticket" is a request to be taught,
    # not a request to file one.
    if _PLAIN_QUESTION.match(text):
        logger.debug("Pre-filter treated %r as a question; skipping the model", text[:60])
        return None, {}

    if _TICKET_REQUEST.search(text):
        summary = _TICKET_PREAMBLE.sub("", text).strip(" ,.;:-")
        # A dangling preposition left by stripping the request phrase reads
        # badly as a ticket title: "for the failing smoke run" should be
        # "the failing smoke run".
        summary = re.sub(r"^(?:for|about|regarding|re|on|saying|that)\b[\s,:-]*",
                         "", summary, flags=re.IGNORECASE).strip(" ,.;:-")
        # Nothing but the request itself was said — keep the original text so
        # the ticket is not filed with an empty or meaningless title.
        if len(summary) < 8:
            summary = text
        logger.info("Pre-filter recognised a ticket request: %r", summary[:80])
        return "raise_ticket", {"summary": summary, "category": "how-to"}

    return None


def interpret(instruction: str, provider) -> dict:
    """
    Turn an instruction into a proposal, executing nothing.

    Args:
        instruction: What the user typed.
        provider: LLM provider. Native tool calling is used when available.

    Returns:
        {"proposal": {...}, "summary": "...", "confirm_fields": [...]}
        or {"needs_clarification": True, "question": ..., "options": [...]}
        or {"not_an_action": True} when the model judges it a question
        or {"error": "..."}
    """
    cat = catalogue()

    decided = _prefilter(instruction)
    if decided is not None:
        name, args = decided
    else:
        name, args = _ask_model(instruction, cat, provider)

    if not name:
        # The model declined to call anything — it judged this a question,
        # not an instruction. Fall through to the answering path.
        return {"not_an_action": True}

    action = ACTIONS.get(name)
    if not action:
        logger.warning("Model asked for an action that does not exist: %r", name)
        return {"error": f"'{name}' is not something I can do."}

    # Normalise before validating. Asking a small model to turn "9 PM" into
    # "21:00" is the wrong division of labour — it returns "today at 9 PM"
    # verbatim and a strict schema then rejects a perfectly clear request.
    # The model works out the INTENT; code does the string handling.
    if "time" in args or "day" in args:
        day = parse_day(args.get("day"), args.get("time"))
        t = parse_time(args.get("time"))
        if t:
            args["time"] = t
        elif args.get("time"):
            args.pop("time")            # unreadable — fall back to the default
        if day:
            args["day"] = day
        elif args.get("day"):
            args.pop("day")

    problem = validate(name, args)
    if problem:
        return {"error": problem}

    resolution: Resolution = action.resolve(args, cat)

    if resolution.error:
        return {"error": resolution.error}
    if resolution.question:
        return {
            "needs_clarification": True,
            "question": resolution.question,
            "options": resolution.options,
        }

    proposal = {"action": name, **resolution.args}
    logger.info("Interpreted %r -> %s", instruction[:60], name)
    return {
        "proposal": proposal,
        "summary": resolution.summary,
        "confirm_fields": list(action.confirm_fields),
    }


def resummarise(proposal: dict) -> str:
    """
    Rebuild the confirmation sentence after the user edits a field.

    Without this, changing the frequency on the card left the sentence — and
    the derived schedule name — describing the original reading.
    """
    fn = SUMMARISERS.get(proposal.get("action"))
    if not fn:
        return ""
    p = dict(proposal)
    if p.get("action") == "create_schedule" and p.get("suite_name"):
        p["name"] = f"{p['suite_name']} — {p.get('frequency')}"
    return fn(p)


def execute(proposal: dict, triggered_by: str = "chat") -> dict:
    """
    Carry out a proposal a person has confirmed.

    Reached only after the proposal was shown and agreed to. Calls the same
    functions the UI calls, so there is no privileged path and the audit
    record is identical to someone clicking the button.
    """
    name = proposal.get("action")
    action = ACTIONS.get(name)
    if not action:
        return {"error": "Unknown action."}
    logger.info("Executing %s (%s)", name, triggered_by)
    return action.execute(proposal, triggered_by)


# ── talking to the model ─────────────────────────────────────────────────

_SYSTEM = """You help a test automation tool carry out instructions.

Call one of the provided functions when the user wants something DONE.

A polite request is still a request. All of these are INSTRUCTIONS and must
result in a function call, even though they are phrased as questions:

    "can you run the smoke suite?"
    "could you schedule smoke for tonight at 9?"
    "would you pause that schedule?"
    "can u schedule smoke run for Nethra Local today at 9 PM?"

Only reply with the single word QUESTION when the user is asking about the
CURRENT STATE and wants information back — how many tests exist, what is
failing, how healthy something is, which tests are flaky. A question mark
alone does not make something a question: what matters is whether they want
an answer or an action.

Use names exactly as they appear in the lists below. Never invent one.

Call the function directly if you can. If you cannot, reply with JSON of
this exact shape instead and nothing else — both are accepted, so a model
that does not emit tool calls is not left with no way to answer:

    {"action": "create_schedule",
     "arguments": {"suite": "...", "frequency": "once", "time": "21:00"}}

APPLICATIONS:
__APPS__

SUITES:
__SUITES__

SCHEDULES:
__SCHEDULES__
"""


def _ask_model(instruction: str, cat: dict, provider) -> tuple[Optional[str], dict]:
    """
    Ask the model which action to take.

    Native tool calling is tried first. Smaller local models are inconsistent
    at emitting it, so a JSON reply is accepted as a fallback — the same
    information in a different envelope. The caller cannot tell which
    happened, which is the point of putting it here.
    """
    system = (
        _SYSTEM
        .replace("__APPS__", _render(cat["applications"],
                                     lambda a: f'"{a["name"]}"  (url: {a["url"]})'))
        .replace("__SUITES__", _render(cat["suites"],
                                       lambda s: f'"{s["name"]}"  ({s["kind"]}, on {s["application"]})'))
        .replace("__SCHEDULES__", _render(cat["schedules"],
                                          lambda s: f'"{s["name"]}"  (on {s["application"]}, '
                                                    f'{"active" if s["enabled"] else "paused"})'))
    )

    tools = provider.build_tool(tool_definitions())
    contents = [provider.user_text_content(instruction)]

    try:
        response = provider.call_model(system, tools, contents)
    except Exception as e:
        logger.exception("Model call failed while interpreting an instruction")
        raise RuntimeError(f"The model could not be reached: {e}") from e

    # 1. native tool call
    try:
        name, args = provider.extract_function_call(response)
        if name:
            logger.info("Model called %s natively with %s", name, dict(args or {}))
            return name, dict(args or {})
    except Exception:
        pass

    # 2. JSON in the text, for models that will not emit a tool call
    text = " ".join(p.text for p in response.candidates[0].content.parts
                    if getattr(p, "text", "")).strip()

    if text.upper().startswith("QUESTION"):
        return None, {}

    parsed = _parse_json(text)
    if isinstance(parsed, dict):
        name = parsed.get("action") or parsed.get("name") or parsed.get("function")
        if name in ACTIONS:
            args = parsed.get("arguments") or parsed.get("args") or {
                k: v for k, v in parsed.items()
                if k not in ("action", "name", "function", "arguments", "args")
            }
            logger.info("Model returned %s as JSON (no native tool call)", name)
            return name, dict(args)

    logger.info("No tool call and no parseable JSON; treating as a question. "
                "Model said: %r", text[:200])
    return None, {}


def _render(items: list, fmt) -> str:
    return "\n".join(f"- {fmt(i)}" for i in items) or "- none"


def _parse_json(text: str) -> Optional[dict]:
    """Parse a JSON reply, tolerating code fences or surrounding prose."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None