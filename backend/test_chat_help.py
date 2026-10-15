"""
test_chat_help.py

Gate test for help grounding in api/routes/chat.py.

Split into two parts:

  Part A, offline. Checks the intent gate and prompt assembly with no model
      and no database. Fast, deterministic, and the part that actually catches
      regressions.

  Part B, live. Sends real questions to the running backend and shows what the
      local model answered and which sources it was given. The model's exact
      wording cannot be asserted, so this part reports rather than judges,
      except for the checks that do not depend on phrasing: that sources are
      returned for a how-to question, and not attached to a data question.

Start the backend first, then from the backend directory:

    python test_chat_help.py

Part B is skipped automatically if the backend is not reachable.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from api.routes import chat as chat_routes
from core.settings import settings


BASE = f"http://127.0.0.1:{settings.app.port}/v1"
USER = "selftest-chat"

_PRECISE = [{
    "citation": "Scheduling runs (v1)", "heading": "Pausing a schedule",
    "body": "Switch it off and it stays in place with all its settings.",
    "match_type": "precise", "source_type": "article",
    "label": "scheduling-runs", "title": "Scheduling runs", "version": 1,
}]
_BROAD = [dict(_PRECISE[0], match_type="broad")]


def _check(failures: list[str], label: str, condition: bool, detail: str = "") -> None:
    """Record the outcome of one assertion.

    Args:
        failures: Accumulator for failure descriptions.
        label: What was being checked.
        condition: Whether it held.
        detail: Extra context printed on failure.
    """
    print(f"    {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(f"{label}{f' - {detail}' if detail else ''}")


def part_a(failures: list[str]) -> None:
    """Offline checks of the intent gate and prompt assembly.

    Args:
        failures: Accumulator for failure descriptions.
    """
    print("\n  A1. when help is attached to a question")

    original = chat_routes.help_service.search
    try:
        cases = [
            ("how do I stop a schedule running every night", _BROAD, True,
             "how-to shape, weak match"),
            ("how many P0 tests are failing", _BROAD, False,
             "data question, weak match"),
            ("how many tests reference scheduling runs", _PRECISE, True,
             "strong match stands on its own"),
            ("what is a test suite", _BROAD, True, "how-to shape"),
            ("show me flaky tests", _BROAD, False, "data question"),
            ("coverage for Netra", [], False, "nothing matched"),
        ]
        for question, returns, expect, why in cases:
            chat_routes.help_service.search = lambda *a, **k: returns
            got = bool(chat_routes._help_passages(question, USER))
            _check(failures, f"{why}: {'attached' if expect else 'not attached'}",
                   got == expect, f"{question!r} -> {got}")

        print("\n  A2. failure isolation")
        def _boom(*a, **k):
            raise RuntimeError("help index unavailable")
        chat_routes.help_service.search = _boom
        _check(failures, "a help failure does not break chat",
               chat_routes._help_passages("how do I schedule a run", USER) == [])
    finally:
        chat_routes.help_service.search = original

    print("\n  A3. prompt and sources")
    block = chat_routes._help_block(_PRECISE)
    _check(failures, "passages are numbered for citation", "[1]" in block, block[:120])
    _check(failures, "the version is in the citation", "(v1)" in block, block[:120])
    _check(failures, "the passage text is included",
           "stays in place" in block, block[:160])
    _check(failures, "nothing is rendered when there are no passages",
           chat_routes._help_block([]) == "")

    sources = chat_routes._sources(_PRECISE)
    _check(failures, "sources are numbered to match the citations",
           sources and sources[0]["n"] == 1)
    _check(failures, "sources carry the article slug",
           sources[0]["label"] == "scheduling-runs")
    _check(failures, "sources omit the body text",
           "body" not in sources[0], str(sources[0]))


def _ask(question: str):
    """Send one question to the running chat endpoint.

    Args:
        question: The question to ask.

    Returns:
        (status_code, decoded_body), or (None, reason) if unreachable.
    """
    req = urllib.request.Request(
        f"{BASE}/chat", method="POST",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json", "X-Triggered-By": USER},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def part_b(failures: list[str]) -> None:
    """Live checks against the running backend and local model.

    Args:
        failures: Accumulator for failure descriptions.
    """
    print("\n  B1. live answers")
    status, out = _ask("how do I stop a schedule from running every night?")
    if status is None:
        print(f"    SKIP  backend not reachable at {BASE} ({out})")
        return

    _check(failures, "chat responds", status == 200 and isinstance(out, dict),
           str(out)[:160])
    if not isinstance(out, dict):
        return

    print(f"\n      Q: how do I stop a schedule from running every night?")
    print(f"      A: {out.get('answer', '')[:400]}")
    print(f"      sources: {[s['citation'] for s in out.get('sources', [])]}")

    _check(failures, "a how-to question is given help sources",
           bool(out.get("sources")), json.dumps(out.get("sources"))[:160])

    status, out = _ask("how many tests are failing right now?")
    print(f"\n      Q: how many tests are failing right now?")
    print(f"      A: {str(out.get('answer', ''))[:300] if isinstance(out, dict) else out}")
    if isinstance(out, dict):
        print(f"      sources: {[s['citation'] for s in out.get('sources', [])]}")
        _check(failures, "a data question is answered without help sources",
               not out.get("sources"), json.dumps(out.get("sources"))[:160])

    q = "how do I export a Kavach dashboard to PowerPoint?"
    status, out = _ask(q)
    print(f"\n      Q: {q}")
    print(f"      A: {str(out.get('answer', ''))[:400] if isinstance(out, dict) else out}")
    print("      (the help content does not cover this — the answer should say so,")
    print("       and the question should now appear as a documentation gap)")

    status, gaps = _ask_gaps()
    if isinstance(gaps, list):
        _check(failures, "the uncovered question was recorded as a gap",
               any("powerpoint" in g["query"].lower() for g in gaps),
               json.dumps(gaps[:3])[:200])


def _ask_gaps():
    """Read the recorded documentation gaps.

    Returns:
        (status_code, decoded_body), or (None, reason) if unreachable.
    """
    req = urllib.request.Request(f"{BASE}/help/gaps",
                                 headers={"X-Triggered-By": USER})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    failures: list[str] = []
    print(f"Backend for live checks: {BASE}")
    part_a(failures)
    part_b(failures)

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())