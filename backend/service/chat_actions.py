"""
service/chat_actions.py

Turning an instruction into an action, safely.

The chat can already answer questions. This lets it DO things — run a suite,
create a schedule, pause one — without ever letting the model touch the
database or decide on its own.

The rules that make this safe are all in the shape of the flow:

  1. The model returns a structured INTENT, never SQL and never code.
  2. Only actions in ACTION_REGISTRY exist. Anything else is refused, so
     what the system can be made to do is enumerable.
  3. Our code — not the model — resolves names to ids and validates them.
     "Netra" is ambiguous when two applications match; we ask rather than
     guess.
  4. Nothing executes until a person confirms. The proposal is returned,
     shown, and only then executed.
  5. Execution calls the same endpoints the UI calls. There is no
     privileged path, and the audit record is identical to a person doing
     it by hand.

Read and write are kept apart: questions go through chat.py untouched, so
a question can never mutate anything.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from core.db.connection import get_conn

logger = logging.getLogger(__name__)


# Every action the chat can perform. Nothing outside this exists.
ACTION_REGISTRY = {
    "run_suite": {
        "label": "Run a test suite",
        "needs": ["application", "suite"],
    },
    "create_schedule": {
        "label": "Create a schedule",
        "needs": ["application", "suite", "frequency", "time"],
    },
    "pause_schedule": {
        "label": "Pause a schedule",
        "needs": ["schedule"],
    },
    "resume_schedule": {
        "label": "Resume a schedule",
        "needs": ["schedule"],
    },
}

# A WIDE net, not a decision. Anything containing one of these words is sent
# to the model, which decides whether it is really an instruction and can
# reply "not an action". Matching only a LEADING verb was too narrow — "can
# you run the smoke suite" and "I want it to run every morning" both missed,
# so the model never saw them and the feature silently did nothing.
#
# A false positive here costs one extra model call. A false negative costs
# the feature, so the net is deliberately loose.
ACTION_WORDS = (
    "run", "execute", "schedule", "scheduled", "scheduling", "pause", "paused",
    "resume", "stop", "start", "create", "set up", "setup", "kick off",
    "kickoff", "trigger", "disable", "enable", "fire off", "launch",
    "every day", "every weekday", "every morning", "every week", "daily",
    "nightly", "weekly", "hourly", "each morning", "automate this",
)

EXTRACT_PROMPT = """Turn this instruction into a structured action.

INSTRUCTION:
__INSTRUCTION__

APPLICATIONS THAT EXIST:
__APPS__

SUITES THAT EXIST:
__SUITES__

SCHEDULES THAT EXIST:
__SCHEDULES__

Allowed actions, and nothing else:
  run_suite         run a suite NOW, once
  create_schedule   set a suite to run repeatedly from now on.
                    "schedule the smoke suite" means create_schedule, with
                    the SUITE name — not the name of an existing schedule.
  pause_schedule    stop an EXISTING schedule. Only when the instruction
                    clearly refers to one that already exists.
  resume_schedule   let a paused schedule run again

Rules:
- Use names EXACTLY as they appear in the lists above. Do not invent one.
- If this is a QUESTION rather than an instruction — asking about state
  rather than telling you to do something — return
  {"action": null, "reason": "question"}. You are the decision here; the
  message reached you only because it might be an instruction.
- If it is an instruction but does not match one of those four actions,
  return {"action": null, "reason": "why"}.
- frequency must be one of: once, hourly, daily, weekdays, weekly.
  "today", "tonight", "tomorrow", "at 9pm", "in an hour" and any single
  point in time mean ONCE.
  "every day", "daily", "nightly", "every weekday", "each morning",
  "every Monday" mean the matching recurring value.
  If the instruction does not clearly say it repeats, use ONCE — an
  unwanted single run is a smaller mistake than an unwanted daily job.
- time must be 24-hour HH:MM. "9pm" is "21:00", "6am" is "06:00".
  If no time is given, use "09:00".
- day: "today", "tomorrow", or null. Use it only if the instruction says so.
- Leave a field null rather than guessing at it.

Return ONLY this JSON, no prose, no markdown fences:
{"action": "create_schedule", "application": "...", "suite": "...",
 "schedule": null, "frequency": "once", "time": "21:00", "day": "today"}
"""


def might_be_instruction(text: str) -> bool:
    """
    Should this message be shown to the model as a possible instruction?

    This is a filter, NOT a decision. It exists only so that plainly
    conversational messages do not pay for an extra model call. Anything
    that could plausibly be an instruction is passed on, and the model
    decides — it can and does reply "not an action".

    An earlier version matched only a leading verb, which meant "can you run
    the smoke suite" and "I want it to run every morning" never reached the
    model at all. The feature appeared to do nothing for most natural
    phrasing. Recall matters far more than precision here.
    """
    t = (text or "").strip().lower()
    if not t:
        return False

    # A question about state is not an instruction, even if it contains a
    # verb: "how many tests run daily" is asking, not telling.
    if t.startswith(("how ", "what ", "which ", "why ", "when ", "who ",
                     "is ", "are ", "do we", "does ", "show me", "tell me")):
        return False

    return any(w in t for w in ACTION_WORDS)


# Kept so any older caller does not break.
looks_like_instruction = might_be_instruction


def _catalogue() -> dict:
    """
    The names the model is allowed to use.

    Passing the real catalogue means the model chooses from what exists
    instead of inventing a plausible-sounding suite that then fails to
    resolve.
    """
    out = {"applications": [], "suites": [], "schedules": []}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, base_url FROM applications "
                "WHERE is_archived = FALSE ORDER BY name"
            )
            out["applications"] = [
                {"id": r[0], "name": r[1], "url": r[2]} for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT s.id, s.name, s.kind, a.name, a.id
                   FROM test_suites s JOIN applications a ON a.id = s.application_id
                   ORDER BY a.name, s.name"""
            )
            out["suites"] = [
                {"id": r[0], "name": r[1], "kind": r[2],
                 "application": r[3], "application_id": r[4]}
                for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT s.id, s.name, s.enabled, a.name
                   FROM schedules s JOIN applications a ON a.id = s.application_id
                   ORDER BY s.name"""
            )
            out["schedules"] = [
                {"id": r[0], "name": r[1], "enabled": r[2], "application": r[3]}
                for r in cur.fetchall()
            ]
    return out



def _clean_name(value: str) -> str:
    """
    Reduce a name the model returned to something matchable.

    It tends to echo back the decoration it was shown — "Smoke Suite 1 run
    on Nethra Local (paused)" instead of "Smoke Suite 1 run". Rather than
    hoping for a clean string, strip the parts we know we added.
    """
    v = (value or "").strip().strip('"').strip("'")
    v = re.sub(r"\s*\([^)]*\)\s*$", "", v)          # trailing "(paused)"
    v = re.sub(r"\s+on\s+.+$", "", v, flags=re.I)     # trailing "on <app>"
    v = re.sub(r"^name:\s*", "", v, flags=re.I)
    return v.strip()


def _best_match(value: str, items: list, key: str = "name") -> list:
    """
    Find the items a name could refer to, trying progressively looser rules.

    Exact first, then containment either way. Returning a list keeps the
    ambiguous case visible so the caller can ask rather than pick.
    """
    v = _clean_name(value).lower()
    if not v:
        return []
    exact = [i for i in items if i[key].lower() == v]
    if exact:
        return exact
    contains = [i for i in items if v in i[key].lower() or i[key].lower() in v]
    if contains:
        return contains
    # last resort: significant words in common
    words = {w for w in re.split(r"\W+", v) if len(w) > 2}
    scored = []
    for i in items:
        iw = {w for w in re.split(r"\W+", i[key].lower()) if len(w) > 2}
        overlap = len(words & iw)
        if overlap:
            scored.append((overlap, i))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:3]] if scored else []


def interpret(instruction: str, provider) -> dict:
    """
    Work out what the user is asking for, and check it can be done.

    Nothing is executed here. The result is a proposal for a person to
    confirm, or a question back when the instruction is ambiguous.

    Args:
        instruction: What the user typed.
        provider: LLM provider used only to extract the intent.

    Returns:
        {"proposal": {...}, "summary": "...", "needs_clarification": bool,
         "question": "...", "error": "..."}
    """
    cat = _catalogue()

    prompt = (
        EXTRACT_PROMPT
        .replace("__INSTRUCTION__", instruction)
        .replace("__APPS__", "\n".join(
            f'- name: "{a["name"]}"   (url: {a["url"]})'
            for a in cat["applications"]) or "- none")
        # Names are quoted and context is labelled, so the model copies the
        # NAME rather than the whole decorated line. It previously returned
        # "Smoke Suite 1 run on Nethra Local (paused)" as a schedule name.
        .replace("__SUITES__", "\n".join(
            f'- name: "{s["name"]}"   (kind: {s["kind"]}, application: {s["application"]})'
            for s in cat["suites"]) or "- none")
        .replace("__SCHEDULES__", "\n".join(
            f'- name: "{s["name"]}"   (application: {s["application"]}, '
            f'{"active" if s["enabled"] else "paused"})'
            for s in cat["schedules"]) or "- none")
    )

    try:
        tool = provider.build_tool([])
        resp = provider.call_model("You reply with JSON only.", tool,
                                   [provider.user_text_content(prompt)])
        text = " ".join(p.text for p in resp.candidates[0].content.parts
                        if getattr(p, "text", "")).strip()
        intent = _parse_json(text)
    except Exception as e:
        logger.exception("Could not interpret the instruction")
        return {"error": f"Could not interpret that: {e}"}

    if not intent or not intent.get("action"):
        reason = (intent or {}).get("reason", "")
        if reason == "question":
            # The model judged this a question. Fall through to the normal
            # answering path rather than showing an error.
            return {"not_an_action": True}
        return {
            "error": "I could not turn that into an action.",
            "detail": reason or
                      "Try: run the smoke suite for Netra, or "
                      "schedule the smoke suite every weekday at 6am.",
        }

    action = intent["action"]
    if action not in ACTION_REGISTRY:
        # The model returned something outside the registry. Refuse rather
        # than attempt it — the whole point is that the set is enumerable.
        logger.warning("Refused unknown action from model: %r", action)
        return {"error": f"'{action}' is not something I can do."}

    return _resolve(action, intent, cat)


def _resolve(action: str, intent: dict, cat: dict) -> dict:
    """
    Turn names into ids, and refuse anything that does not check out.

    This is our code, not the model's. A name that matches two things is a
    question, not a coin toss — you have two applications whose names both
    contain "Netra", and picking one silently would be exactly the failure
    this is meant to prevent.
    """
    proposal = {"action": action}

    if action in ("pause_schedule", "resume_schedule"):
        matches = _best_match(intent.get("schedule"), cat["schedules"])
        if not matches:
            return {"error": f"I could not find a schedule called '{intent.get('schedule')}'."}
        if len(matches) > 1:
            return {
                "needs_clarification": True,
                "question": "Which schedule do you mean?",
                "options": [s["name"] for s in matches],
            }
        s = matches[0]
        proposal.update({"schedule_id": s["id"], "schedule_name": s["name"]})
        verb = "Pause" if action == "pause_schedule" else "Resume"
        return {"proposal": proposal, "summary": f"{verb} the schedule '{s['name']}'."}

    # run_suite and create_schedule both need an application and a suite
    app_name = _clean_name(intent.get("application")).lower()
    apps = _best_match(intent.get("application"), cat["applications"]) if app_name else []
    if app_name and not apps:
        return {"error": f"I could not find an application called '{intent.get('application')}'."}
    if len(apps) > 1:
        # Longest match wins only if it is unambiguous; otherwise ask.
        exact = [a for a in apps if a["name"].lower() == app_name]
        if len(exact) == 1:
            apps = exact
        else:
            return {
                "needs_clarification": True,
                "question": f"'{intent.get('application')}' matches more than one application. Which one?",
                "options": [a["name"] for a in apps],
            }

    suite_name = _clean_name(intent.get("suite")).lower()
    suites = cat["suites"]
    if apps:
        suites = [s for s in suites if s["application_id"] == apps[0]["id"]]
    matches = _best_match(intent.get("suite"), suites) if suite_name else []

    if not matches and suite_name:
        where = f" on {apps[0]['name']}" if apps else ""
        return {"error": f"I could not find a suite called '{intent.get('suite')}'{where}."}
    if not matches and len(suites) == 1:
        matches = suites          # only one suite — no ambiguity to resolve
    if not matches:
        return {
            "needs_clarification": True,
            "question": "Which suite?",
            "options": [f"{s['name']} ({s['application']})" for s in suites][:8],
        }
    if len(matches) > 1:
        return {
            "needs_clarification": True,
            "question": f"'{intent.get('suite')}' matches more than one suite. Which one?",
            "options": [f"{s['name']} ({s['application']})" for s in matches][:8],
        }

    suite = matches[0]
    proposal.update({
        "suite_id": suite["id"], "suite_name": suite["name"],
        "application_id": suite["application_id"],
        "application_name": suite["application"],
    })

    if action == "run_suite":
        return {
            "proposal": proposal,
            "summary": f"Run '{suite['name']}' on {suite['application']} now, headless.",
        }

    # create_schedule
    # Default to a one-off. Creating a recurring job someone did not ask for
    # is the worse mistake, and it is the one that runs again tomorrow.
    freq = (intent.get("frequency") or "once").lower()
    if freq not in ("once", "hourly", "daily", "weekdays", "weekly"):
        return {"error": f"'{freq}' is not a frequency I support."}

    hhmm = (intent.get("time") or "09:00").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", hhmm):
        return {"error": f"I could not read '{hhmm}' as a time."}
    hour, minute = (int(x) for x in hhmm.split(":"))
    if hour > 23 or minute > 59:
        return {"error": f"'{hhmm}' is not a valid time."}

    first = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = (intent.get("day") or "").lower()
    if day == "tomorrow":
        first += timedelta(days=1)
    elif first <= datetime.now():
        # The time has already passed today, so the earliest it can mean is
        # tomorrow — unless they explicitly said today, in which case it is
        # an instruction that cannot be honoured.
        if day == "today":
            return {"error": f"{hhmm} has already passed today."}
        first += timedelta(days=1)
    if freq == "weekdays":
        while first.weekday() >= 5:
            first += timedelta(days=1)

    proposal.update({
        "frequency": freq,
        "run_at": first.isoformat(),
        "name": f"{suite['name']} — {freq}",
    })
    return {
        "proposal": proposal,
        "summary": (
            f"Create a schedule: run '{suite['name']}' on {suite['application']} "
            f"{_freq_words(freq)} at {hhmm}. First run "
            f"{first.strftime('%a %d %b, %H:%M')}."
        ),
    }


def execute(proposal: dict, triggered_by: str = "chat") -> dict:
    """
    Carry out a proposal that a person has confirmed.

    Calls the same functions the UI calls, so there is no privileged path
    and the audit record is identical to someone clicking the button.

    Args:
        proposal: Exactly what was shown and confirmed.
        triggered_by: Recorded against the resulting run.
    """
    action = proposal.get("action")
    if action not in ACTION_REGISTRY:
        return {"error": "Unknown action."}

    if action == "run_suite":
        from api.routes.suites import run_suite, RunSuiteRequest
        logger.info("Chat action: running suite %s", proposal["suite_id"])
        r = run_suite(proposal["suite_id"],
                      RunSuiteRequest(headed=False, triggered_by=triggered_by))
        if r.get("error"):
            return r
        return {
            "done": True,
            "message": (
                f"{r['suite']}: {r['passed']} passed, {r['failed']} failed"
                + (f", {r['skipped']} skipped" if r.get("skipped") else "")
            ),
            "result": r,
        }

    if action == "create_schedule":
        from api.routes.schedules import create_schedule, ScheduleIn
        from fastapi import Request  # noqa: F401  (only for the type)

        class _Req:                       # minimal stand-in for the header read
            headers = {"X-Triggered-By": triggered_by}

        logger.info("Chat action: creating schedule for suite %s", proposal["suite_id"])
        r = create_schedule(ScheduleIn(
            application_id=proposal["application_id"],
            suite_id=proposal["suite_id"],
            name=proposal["name"],
            frequency=proposal["frequency"],
            run_at=proposal["run_at"],
        ), _Req())
        if r.get("error"):
            return r
        return {
            "done": True,
            "message": f"Schedule '{proposal['name']}' created.",
            "result": r,
        }

    if action in ("pause_schedule", "resume_schedule"):
        from api.routes.schedules import update_schedule, SchedulePatch
        enabled = action == "resume_schedule"
        r = update_schedule(proposal["schedule_id"], SchedulePatch(enabled=enabled))
        if r.get("error"):
            return r
        word = "resumed" if enabled else "paused"
        return {"done": True, "message": f"'{proposal['schedule_name']}' {word}.", "result": r}

    return {"error": "Unknown action."}


def _parse_json(text: str) -> Optional[dict]:
    """Parse the model's JSON, tolerating fences or surrounding prose."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _freq_words(freq: str) -> str:
    return {
        "once": "once", "hourly": "every hour", "daily": "every day",
        "weekdays": "every weekday", "weekly": "every week",
    }.get(freq, freq)