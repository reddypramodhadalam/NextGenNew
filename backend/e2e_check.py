"""
e2e_check.py

Drive the whole pipeline from the backend, with no frontend and no uvicorn:

    survey -> accept -> generate script -> execute -> read the step ledger

Calls the same orchestrator functions the API calls, so a pass here means
the real path works, not a parallel one written for testing.

Usage:
    python e2e_check.py <application_id> <url> [brd.docx] [--keep] [--cases N]

    python e2e_check.py 12 https://www.saucedemo.com
    python e2e_check.py 12 https://www.saucedemo.com ../sample_docs/SwagLabs_BRD.docx
    python e2e_check.py 12 https://www.saucedemo.com --cases 2 --keep

UNLIKE preview_survey.py, THIS WRITES TO THE DATABASE. It creates real
proposals, generations and executions. By default it deletes everything it
created before exiting; pass --keep to leave the rows in place so you can
look at them in the UI.

Expect a few minutes: one model call per requirement chunk to survey, then
roughly a minute per test case to generate a script, then a real browser run.
"""

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from core.db.connection import init_pool, get_conn          # noqa: E402
from core.db.queries import proposals as proposals_db       # noqa: E402
from service.orchestrator import (                          # noqa: E402
    run_generate_scripts, _extract_text_from_document, _make_driver,
    _resolve_target_url,
)
from service.fast_generation import fast_survey             # noqa: E402
from llm.provider_factory import get_provider               # noqa: E402
from service.selenium_runner import run_script_file         # noqa: E402
from core.db.queries import executions as executions_db     # noqa: E402
from service.test_case_import import flatten_steps          # noqa: E402

# Title prefix, so cleanup only ever removes our rows. Deliberately free of
# brackets and punctuation: the prefix ends up in the generated script's
# FILENAME, and pytest cannot collect a path containing [].
MARKER = "ZZE2E"


async def _noop(event_type, data):
    """Progress sink — prints only the lines worth watching."""
    msg = (data or {}).get("message")
    if msg and event_type in ("status", "step"):
        print(f"    {msg[:100]}")


def _cleanup(proposal_ids: list) -> None:
    """Remove everything this run created, children first."""
    if not proposal_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM executions WHERE generation_id IN
                           (SELECT id FROM generations
                             WHERE test_case_proposal_id = ANY(%s))""", (proposal_ids,))
            cur.execute("DELETE FROM generations WHERE test_case_proposal_id = ANY(%s)",
                        (proposal_ids,))
            cur.execute("DELETE FROM proposal_edits WHERE test_case_proposal_id = ANY(%s)",
                        (proposal_ids,))
            cur.execute("DELETE FROM test_case_proposals WHERE id = ANY(%s)", (proposal_ids,))
    print(f"\nCleaned up {len(proposal_ids)} test case(s) and everything under them.")


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    limit = 1
    if "--cases" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--cases") + 1])

    if len(args) < 2:
        print(__doc__)
        return 2
    application_id, url = int(args[0]), args[1]
    brd = args[2] if len(args) > 2 else None

    init_pool()
    created: list = []

    try:
        # ── 1. SURVEY ──────────────────────────────────────────────────
        print("=" * 68)
        print("1. SURVEY")
        print("=" * 68)
        doc_text = _extract_text_from_document(brd) if brd else None
        if brd and not doc_text:
            print(f"  Could not read {brd} — continuing without it.")

        # Survey directly rather than through run_new_test_suite, which
        # finds-or-creates an application from the URL. Passing a name it
        # does not recognise left a junk application behind on every run,
        # and cleanup could not remove it without risking a real one.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, base_url FROM applications WHERE id = %s",
                            (application_id,))
                row = cur.fetchone()
        if not row:
            print(f"  No application with id {application_id}.")
            return 2
        app_name, app_base_url = row
        print(f"  application {application_id}: {app_name}")

        # Survey and execution use `url`; generation resolves its own from the
        # application. This harness let those diverge silently, which hid a
        # generate-here/run-there mismatch for six full runs. Fail fast.
        gen_url = _resolve_target_url(
            {"name": app_name, "base_url": app_base_url}, "default")
        print(f"  survey/execute URL : {url}")
        print(f"  generation URL     : {gen_url}")
        if gen_url != url:
            print()
            print("  ABORTING — the three stages do not agree on the URL.")
            print(f"    survey and execution : {url}")
            print(f"    generation resolves  : {gen_url}")
            print(f"    application base_url : {app_base_url}")
            print("  Scripts would be generated for one site and run against")
            print("  another. Pass the application's own base_url, or fix")
            print("  config/applications.yaml.")
            return 2
        print(f"  all three stages agree on {url}")

        driver = _make_driver()
        try:
            driver.get(url)
            survey = fast_survey(
                driver=driver, url=url, provider=get_provider(),
                document_context=doc_text,
                on_progress=lambda d, t, label: print(f"    [{d}/{t}] {label}"),
            )
        finally:
            driver.quit()

        result = {"proposals": survey.get("test_cases", [])}
        report = survey.get("requirement_report") or {}
        if report.get("zero_coverage"):
            print(f"  requirements with no test case: {', '.join(report['zero_coverage'])}")
        proposed = result.get("proposals", [])
        stepped = [p for p in proposed if p.get("steps")]
        print(f"\n  proposed: {len(proposed)}   with steps: {len(stepped)}")
        if not stepped:
            print("  No stepped test cases produced — stopping.")
            return 1

        # ── 2. ACCEPT ──────────────────────────────────────────────────
        print()
        print("=" * 68)
        print(f"2. ACCEPT (first {limit})")
        print("=" * 68)
        for p in stepped[:limit]:
            steps = p["steps"]
            row = proposals_db.create(
                application_id=application_id,
                title=f"{MARKER} {p['title']}"[:200],
                description=flatten_steps(steps),
                source="author_agent",
                category=p.get("category"),
                steps=steps,
            )
            created.append(row["id"])
            print(f"  id={row['id']}  {p['title'][:56]}")
            for s in steps:
                mark = "" if s.get("matched", True) else "   [no matching element]"
                print(f"      {s['n']}. {s['step'][:60]}{mark}")

        # ── 3. GENERATE ────────────────────────────────────────────────
        print()
        print("=" * 68)
        print("3. GENERATE SCRIPTS")
        print("=" * 68)
        gen = await run_generate_scripts(
            application_id=application_id,
            proposal_ids=created,
            environment_key="default",
            triggered_by="e2e_check",
            progress_callback=_noop,
        )
        print(f"\n  generated: {gen.get('generated')}   incomplete: {gen.get('incomplete')}")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id, test_case_proposal_id, script_path, generation_status
                               FROM generations WHERE test_case_proposal_id = ANY(%s)
                               ORDER BY id""", (created,))
                gens = cur.fetchall()
        for gid, pid, path, status in gens:
            print(f"  generation {gid} (proposal {pid}) {status}  {path}")
            if path and Path(path).exists():
                src = Path(path).read_text(encoding="utf-8")
                uses = "kavach_evidence" in src
                print(f"      uses the evidence module: {uses}"
                      f"{'' if uses else '   <-- script will produce no step ledger'}")

        runnable = [(g[0], g[2]) for g in gens if g[2] and Path(g[2]).exists()]
        if not runnable:
            print("\n  No runnable script produced — stopping before execution.")
            return 1

        # ── 4. EXECUTE ─────────────────────────────────────────────────
        print()
        print("=" * 68)
        print("4. EXECUTE")
        print("=" * 68)
        for gid, path in runnable:
            r = run_script_file(path, headed=False, target_url=url)
            executions_db.create(
                generation_id=gid,
                result=r["result"],
                output=r["output"],
                duration_ms=r["duration_ms"],
                screenshot_path=r.get("screenshot_dir"),
                triggered_by="e2e_check",
                environment="default",
                step_results=r.get("step_results"),
            )
            print(f"  generation {gid}: {r['result'].upper()} in {r['duration_ms']}ms")

        # ── 5. WHAT LANDED IN THE DATABASE ─────────────────────────────
        print()
        print("=" * 68)
        print("5. STEP LEDGER, READ BACK FROM THE DATABASE")
        print("=" * 68)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT e.id, e.result, e.step_results, e.screenshot_path
                               FROM executions e
                               WHERE e.generation_id = ANY(%s)
                               ORDER BY e.id""", ([g[0] for g in runnable],))
                rows = cur.fetchall()

        for eid, res, steps, shots in rows:
            print(f"\n  execution {eid}  result={res}")
            if not steps:
                print("    step_results IS NULL  <-- no per-step record was stored")
                continue
            for s in steps:
                img = s.get("screenshot") or "-"
                print(f"    {s['n']}. {s['action'][:40]:42} {s['status']:14} "
                      f"{str(s['duration_ms']):>6}ms  {img}")
                if s.get("error"):
                    print(f"         {s['error'][:88]}")
            shot_files = sorted(Path(shots).glob("*.png")) if shots and Path(shots).exists() else []
            print(f"    screenshots: {len(shot_files)} for {len(steps)} step(s) in {shots}")

        print()
        print("=" * 68)
        print("RESULT: the full chain ran — survey, accept, generate, execute,")
        print("        and per-step evidence stored against the execution.")
        print("=" * 68)
        return 0

    finally:
        if keep:
            print(f"\n--keep: leaving proposals {created} in place.")
        else:
            _cleanup(created)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
