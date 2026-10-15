"""
service/actions/catalog.py

Every action the assistant can perform.

Adding one means adding an Action here and nothing else: the tool schema,
the validation, the confirmation wording and the execution all come from
this single declaration.

The set is deliberately explicit rather than open. In a validated
environment the question "what can an LLM trigger in this system?" needs an
answer you can print, and that answer is this file.
"""

import logging
import re
from datetime import datetime, timedelta

from service.actions.base import Action, Resolution
from service.actions.domain import FREQUENCIES, RELATIVE_DAYS, frequency_words
from service.actions.matching import best_match, clean_name

logger = logging.getLogger(__name__)


# ── run_suite ────────────────────────────────────────────────────────────

def _resolve_run_suite(args: dict, catalogue: dict) -> Resolution:
    """Find the suite meant, refusing rather than guessing when unclear."""
    apps = best_match(args.get("application"), catalogue["applications"])
    if args.get("application") and not apps:
        return Resolution.refuse(
            f"I could not find an application called '{args['application']}'."
        )
    if len(apps) > 1:
        return Resolution.ask(
            f"'{args['application']}' matches more than one application. Which one?",
            [a["name"] for a in apps],
        )

    suites = catalogue["suites"]
    if apps:
        suites = [s for s in suites if s["application_id"] == apps[0]["id"]]

    matches = best_match(args.get("suite"), suites) if args.get("suite") else suites
    if not matches:
        where = f" on {apps[0]['name']}" if apps else ""
        return Resolution.refuse(f"I could not find a suite called '{args.get('suite')}'{where}.")
    if len(matches) > 1:
        return Resolution.ask(
            "Which suite?",
            [f"{s['name']} ({s['application']})" for s in matches][:8],
        )

    s = matches[0]
    return Resolution.done(
        {"suite_id": s["id"], "suite_name": s["name"],
         "application_id": s["application_id"], "application_name": s["application"]},
        f"Run '{s['name']}' on {s['application']} now, headless.",
    )


def _execute_run_suite(args: dict, triggered_by: str) -> dict:
    from api.routes.suites import run_suite, RunSuiteRequest
    r = run_suite(args["suite_id"], RunSuiteRequest(headed=False, triggered_by=triggered_by))
    if r.get("error"):
        return r
    return {
        "done": True,
        "message": (f"{r['suite']}: {r['passed']} passed, {r['failed']} failed"
                    + (f", {r['skipped']} skipped" if r.get("skipped") else "")),
        "result": r,
    }


RUN_SUITE = Action(
    name="run_suite",
    description=(
        "Run a test suite immediately, once. Use for instructions like "
        "'run the smoke suite', 'execute regression for Netra now'. Do not "
        "use when the instruction asks for something repeating or at a later "
        "time — that is create_schedule."
    ),
    parameters={
        "type": "object",
        "properties": {
            "application": {
                "type": "string",
                "description": "Name of the application, exactly as listed. Omit if not stated.",
            },
            "suite": {
                "type": "string",
                "description": "Name of the suite, exactly as listed.",
            },
        },
        "required": ["suite"],
    },
    resolve=_resolve_run_suite,
    execute=_execute_run_suite,
)


# ── create_schedule ──────────────────────────────────────────────────────

def _resolve_create_schedule(args: dict, catalogue: dict) -> Resolution:
    """Resolve the suite, then work out when the first run should be."""
    base = _resolve_run_suite(args, catalogue)
    if not base.ok:
        return base

    freq = (args.get("frequency") or "once").lower()
    if freq not in FREQUENCIES:
        return Resolution.refuse(
            f"'{freq}' is not a frequency I support. "
            f"Use one of: {', '.join(FREQUENCIES)}."
        )

    hhmm = (args.get("time") or "09:00").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", hhmm):
        return Resolution.refuse(f"I could not read '{hhmm}' as a time.")
    hour, minute = (int(x) for x in hhmm.split(":"))
    if hour > 23 or minute > 59:
        return Resolution.refuse(f"'{hhmm}' is not a valid time.")

    now = datetime.now()
    first = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = (args.get("day") or "").lower()

    if day == "tomorrow":
        first += timedelta(days=1)
    elif first <= now:
        if day == "today":
            # Asked for a time that has gone. Say so rather than quietly
            # moving it to tomorrow, which is not what was asked for.
            return Resolution.refuse(f"{hhmm} has already passed today.")
        first += timedelta(days=1)

    if freq == "weekdays":
        while first.weekday() >= 5:
            first += timedelta(days=1)

    a = dict(base.args)
    a.update({
        "frequency": freq,
        "run_at": first.isoformat(),
        "name": f"{base.args['suite_name']} — {freq}",
    })
    return Resolution.done(a, _summarise_schedule(a))


def _summarise_schedule(a: dict) -> str:
    """The sentence shown for confirmation. Recomputed whenever a field changes."""
    d = datetime.fromisoformat(a["run_at"])
    return (
        f"Create a schedule: run '{a['suite_name']}' on {a['application_name']} "
        f"{frequency_words(a['frequency'])} at {d.strftime('%H:%M')}. "
        f"First run {d.strftime('%a %d %b, %H:%M')}."
    )


def _execute_create_schedule(args: dict, triggered_by: str) -> dict:
    from api.routes.schedules import create_schedule, ScheduleIn

    class _Req:
        headers = {"X-Triggered-By": triggered_by}

    r = create_schedule(ScheduleIn(
        application_id=args["application_id"],
        suite_id=args["suite_id"],
        name=args["name"],
        frequency=args["frequency"],
        run_at=args["run_at"],
    ), _Req())
    if r.get("error"):
        return r
    return {"done": True, "message": f"Schedule '{args['name']}' created.", "result": r}


CREATE_SCHEDULE = Action(
    name="create_schedule",
    description=(
        "Set a suite to run automatically, either once at a future time or "
        "repeatedly. Use for 'schedule the smoke suite', 'run regression every "
        "weekday at 6am', 'run smoke tonight at 9'. The 'suite' argument is the "
        "name of a TEST SUITE, never the name of an existing schedule."
    ),
    parameters={
        "type": "object",
        "properties": {
            "application": {"type": "string", "description": "Application name, exactly as listed."},
            "suite": {"type": "string", "description": "Suite name, exactly as listed."},
            "frequency": {
                "type": "string",
                "enum": list(FREQUENCIES),
                "description": (
                    "How often it repeats. A single point in time — 'today', "
                    "'tonight', 'at 9pm' — is 'once'. Only use a repeating value "
                    "when the instruction says it repeats. When unclear use 'once': "
                    "an unwanted single run is a smaller mistake than an unwanted "
                    "daily job."
                ),
            },
            "time": {
                "type": "string",
                "description": "24-hour HH:MM. '9pm' is '21:00', '6am' is '06:00'.",
            },
            "day": {
                "type": "string",
                "enum": list(RELATIVE_DAYS),
                "description": "Only if the instruction says today or tomorrow.",
            },
        },
        "required": ["suite"],
    },
    resolve=_resolve_create_schedule,
    execute=_execute_create_schedule,
    # These may be adjusted on the confirmation card. A schedule is a
    # commitment, so the model's reading is a starting point, not a decision.
    confirm_fields=("frequency", "run_at"),
)


# ── pause_schedule / resume_schedule ─────────────────────────────────────

def _resolve_schedule_state(args: dict, catalogue: dict, resuming: bool) -> Resolution:
    matches = best_match(args.get("schedule"), catalogue["schedules"])
    if not matches:
        return Resolution.refuse(
            f"I could not find a schedule called '{args.get('schedule')}'."
        )
    if len(matches) > 1:
        return Resolution.ask("Which schedule do you mean?", [s["name"] for s in matches])
    s = matches[0]
    verb = "Resume" if resuming else "Pause"
    return Resolution.done(
        {"schedule_id": s["id"], "schedule_name": s["name"], "enabled": resuming},
        f"{verb} the schedule '{s['name']}'.",
    )


def _execute_schedule_state(args: dict, triggered_by: str) -> dict:
    from api.routes.schedules import update_schedule, SchedulePatch
    r = update_schedule(args["schedule_id"], SchedulePatch(enabled=args["enabled"]))
    if r.get("error"):
        return r
    word = "resumed" if args["enabled"] else "paused"
    return {"done": True, "message": f"'{args['schedule_name']}' {word}.", "result": r}


_SCHEDULE_NAME_PARAM = {
    "type": "object",
    "properties": {
        "schedule": {
            "type": "string",
            "description": "Name of an EXISTING schedule, exactly as listed.",
        },
    },
    "required": ["schedule"],
}

PAUSE_SCHEDULE = Action(
    name="pause_schedule",
    description=(
        "Stop an existing schedule from running. Only use when the instruction "
        "clearly refers to a schedule that already exists."
    ),
    parameters=_SCHEDULE_NAME_PARAM,
    resolve=lambda a, c: _resolve_schedule_state(a, c, resuming=False),
    execute=_execute_schedule_state,
)

RESUME_SCHEDULE = Action(
    name="resume_schedule",
    description="Let a paused schedule run again.",
    parameters=_SCHEDULE_NAME_PARAM,
    resolve=lambda a, c: _resolve_schedule_state(a, c, resuming=True),
    execute=_execute_schedule_state,
)


# ── the catalogue ────────────────────────────────────────────────────────

# ── raise_ticket ─────────────────────────────────────────────────────────

# Kept in step with api/routes/support.py::CATEGORIES. Listed here so the
# model is given the vocabulary rather than inventing one, and so an unknown
# value is a fallback rather than a failure.
_TICKET_CATEGORIES = ("how-to", "bug", "data", "access", "request")


def _resolve_raise_ticket(args: dict, catalogue: dict) -> Resolution:
    """Turn a request for help into a ticket ready to file.

    Nothing is looked up. Unlike running a suite, a ticket refers to no
    existing record, so there is nothing to disambiguate — which means the
    only failure worth guarding is an empty summary.

    Args:
        args: Model arguments.
        catalogue: What exists. Unused here, kept for the shared signature.

    Returns:
        A resolution carrying the ticket to raise, or a refusal.
    """
    summary = (args.get("summary") or "").strip()
    if not summary:
        return Resolution.refuse(
            "I need a one-line summary of the problem before I can raise a ticket."
        )

    category = args.get("category")
    if category not in _TICKET_CATEGORIES:
        category = "how-to"

    detail = (args.get("details") or "").strip()

    return Resolution.done(
        {"category": category, "summary": summary, "details": detail},
        f"Raise a support ticket: “{summary}”.",
    )


def _execute_raise_ticket(args: dict, triggered_by: str) -> dict:
    """File the ticket through the same path the Help screen uses.

    Args:
        args: Resolved arguments.
        triggered_by: Who confirmed it.

    Returns:
        A result carrying the external reference, or the reason it could not
        be filed. A failure here still leaves the ticket stored in NextGen2 —
        create_support_ticket writes the row before calling out.
    """
    from api.routes.support import create_support_ticket

    r = create_support_ticket(
        category=args["category"],
        summary=args["summary"],
        description=args.get("details") or "",
        context={"source": "NextGen2-ai", "raised_via": "assistant"},
        raised_by=triggered_by,
    )

    ref = r.get("external_ref") or f"#{r.get('id')}"
    if r.get("error"):
        # Not a failure of the action: the ticket exists. Say what happened
        # rather than implying nothing was recorded.
        return {
            "done": True,
            "message": f"Saved as {ref}, but it could not be filed externally: {r['error']}",
            "result": r,
        }
    return {
        "done": True,
        "message": f"Ticket {ref} raised."
                   + (f" {r['external_url']}" if r.get("external_url") else ""),
        "result": r,
    }


RAISE_TICKET = Action(
    name="raise_ticket",
    description=(
        "Raise a support ticket. Use when the user asks you to raise, file, "
        "log or open a ticket, or asks you to report a problem to someone. "
        "Also use when they ask for help with something and explicitly want "
        "it escalated. Write the summary as one clear line describing their "
        "problem in their own terms. Do NOT use this merely because you "
        "cannot answer a question — only when the user asks for a ticket."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One line describing the problem, in the user's own terms.",
            },
            "category": {
                "type": "string",
                "enum": list(_TICKET_CATEGORIES),
                "description": (
                    "how-to for using a feature, bug for something broken, "
                    "data for wrong results, access for permissions, request "
                    "for something NextGen2 does not do yet."
                ),
            },
            "details": {
                "type": "string",
                "description": "Any further detail the user gave. Omit if none.",
            },
        },
        "required": ["summary"],
    },
    resolve=_resolve_raise_ticket,
    execute=_execute_raise_ticket,
)


ACTIONS = {
    a.name: a for a in (RUN_SUITE, CREATE_SCHEDULE, PAUSE_SCHEDULE,
                        RESUME_SCHEDULE, RAISE_TICKET)
}

# Recomputed summaries for actions whose fields can be edited before
# confirming. Keyed by action name; absent means the summary is fixed.
SUMMARISERS = {
    "create_schedule": _summarise_schedule,
}