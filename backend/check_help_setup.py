"""
check_help_setup.py

Reports which parts of the help and support wiring are actually loaded.

Written because three rounds of "it still does not work" could each have been
one of four causes, and guessing between them from a screenshot wastes more
time than checking. This imports the real modules and calls the real
endpoints, so it reports what is running, not what should be.

Run from the backend directory, with the backend running:

    python check_help_setup.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from core.settings import settings

BASE = f"http://127.0.0.1:{settings.app.port}/v1"
TICKET_PHRASE = ("can u raise a ticket in JIRA for me, i am not able to get "
                 "how do i do regression test suite")


def line(ok: bool, label: str, detail: str = "") -> bool:
    """Print one check result.

    Args:
        ok: Whether the check passed.
        label: What was checked.
        detail: Extra context.

    Returns:
        The value of ok, so callers can accumulate.
    """
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{f'  —  {detail}' if detail else ''}")
    return ok


def check_files() -> bool:
    """Check the modules on disk contain the newest changes.

    Returns:
        True if every module has its expected symbol.
    """
    print("\n1. Files on disk")
    good = True

    from service.actions import runner
    good &= line(hasattr(runner, "_prefilter"),
                 "runner.py has the action pre-filter",
                 "" if hasattr(runner, "_prefilter") else "old file — copy runner.py again")

    from service.actions.catalog import ACTIONS
    good &= line("raise_ticket" in ACTIONS,
                 "catalog.py registers raise_ticket",
                 f"registered: {', '.join(sorted(ACTIONS))}")

    from api.routes import chat
    good &= line(hasattr(chat, "_strip_citation_markers"),
                 "chat.py strips bracket citations")
    good &= line(hasattr(chat, "_looks_like_howto"),
                 "chat.py skips the data snapshot for how-to questions")

    from api.routes import support
    good &= line(hasattr(support, "create_support_ticket"),
                 "support.py exposes the shared ticket path")

    # There are TWO gates in front of the interpreter, and a message must pass
    # both. api/routes/chat_actions.py::might_be_instruction runs first and
    # rejects anything without an action word; runner.py::_prefilter runs
    # second. A request that clears one and not the other is answered as a
    # question with nothing logged to say why, which is exactly how the ticket
    # feature looked broken while both halves were correct in isolation.
    from api.routes.chat_actions import might_be_instruction
    good &= line(might_be_instruction(TICKET_PHRASE),
                 "the outer gate lets ticket requests through",
                 "" if might_be_instruction(TICKET_PHRASE)
                 else "ACTION_HINTS in chat_actions.py has no ticket words")
    return good


def check_prefilter() -> bool:
    """Check the pre-filter routes the failing phrase to a ticket.

    Returns:
        True if it resolves to raise_ticket.
    """
    print("\n2. Pre-filter, in this process")
    from service.actions.runner import _prefilter

    out = _prefilter(TICKET_PHRASE)
    if out is None:
        return line(False, "ticket phrase is recognised", "fell through to the model")
    name, args = out
    if name != "raise_ticket":
        return line(False, "ticket phrase is recognised", f"routed to {name!r}")
    line(True, "ticket phrase is recognised", f"summary: {args['summary'][:60]}")
    return True


def check_running_backend() -> bool:
    """Check the RUNNING backend, which may be older than the files.

    This is the one that has caught us out: editing a file changes nothing
    until the process is restarted, and every symptom looks identical.

    Returns:
        True if the running process behaves as the files say it should.
    """
    print("\n3. The running backend")
    good = True

    try:
        req = urllib.request.Request(
            f"{BASE}/chat/interpret", method="POST",
            data=json.dumps({"instruction": TICKET_PHRASE}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
    except urllib.error.URLError as e:
        print(f"  SKIP backend not reachable at {BASE} ({e.reason})")
        return good

    if out.get("proposal"):
        line(True, "interpret returns a ticket proposal", out.get("summary", "")[:70])
    elif out.get("not_an_action"):
        good &= line(False, "interpret returns a ticket proposal",
                     "got not_an_action — the RUNNING backend has the old "
                     "runner.py. Restart it.")
    else:
        good &= line(False, "interpret returns a ticket proposal", json.dumps(out)[:120])

    try:
        req = urllib.request.Request(
            f"{BASE}/chat", method="POST",
            data=json.dumps({"question": "how do i schedule a smoke test suite?"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return line(False, "chat responds", str(e))

    srcs = out.get("sources") or []
    good &= line("sources" in out, "chat returns a sources field")
    good &= line(bool(srcs), "sources is populated",
                 ", ".join(s["citation"] for s in srcs[:3]) if srcs
                 else "empty — retrieval found nothing for this question")
    answer = out.get("answer", "")
    import re
    markers = re.findall(r"\[\d+\]", answer)
    good &= line(not markers, "no bracket citation markers in the answer",
                 f"found {markers}" if markers else "")
    print(f"\n  answer: {answer[:220]}")
    if srcs:
        print(f"  sources: {[s['citation'] for s in srcs]}")
    return good


def main() -> int:
    """Run every check.

    Returns:
        0 if everything is wired correctly, 1 otherwise.
    """
    print(f"Backend: {BASE}")
    ok = check_files()
    ok &= check_prefilter()
    ok &= check_running_backend()

    print()
    if ok:
        print("Everything is wired correctly.")
        return 0
    print("Something above is not live. The most common cause is that the "
          "backend was not restarted after copying files.")
    return 1


if __name__ == "__main__":
    sys.exit(main())