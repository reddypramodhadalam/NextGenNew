"""
preview_survey.py

Run a survey and PRINT what it produces. Writes nothing — no database rows,
no files, no accept gate. A way to see what the model actually returns for a
URL and a BRD before deciding whether the output is worth accepting.

Usage:
    python preview_survey.py <url> [path/to/requirements.docx]

    python preview_survey.py http://localhost:3000
    python preview_survey.py http://localhost:3000 ..\\BRD.docx

Reads the same .env, provider and prompts the real survey uses, so what you
see here is what the wizard would show.
"""

import sys
import time
from pathlib import Path

# stdout as UTF-8 — the flattened steps contain "→", and the Windows console
# default codepage raises UnicodeEncodeError on it.
sys.stdout.reconfigure(encoding="utf-8")

from llm.provider_factory import get_provider          # noqa: E402
from service.fast_generation import (                  # noqa: E402
    fast_survey, _split_requirements, SURVEY_MAX_CHUNKS, CHUNK_CHARS,
)
from service.app_map_crawler import _make_driver       # noqa: E402
from service.document_extractor import extract_text_from_document as _extract_text_from_document  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    url = sys.argv[1]
    doc_path = sys.argv[2] if len(sys.argv) > 2 else None

    document_context = None
    if doc_path:
        if not Path(doc_path).exists():
            print(f"No such document: {doc_path}")
            return 2
        document_context = _extract_text_from_document(doc_path)
        if not document_context:
            print(f"Could not extract text from {doc_path} — surveying the URL alone.")
        else:
            chunks = _split_requirements(document_context)
            print(f"Document      : {Path(doc_path).name}")
            print(f"Extracted     : {len(document_context):,} characters")
            print(f"Split into    : {len(chunks)} requirement chunk(s) "
                  f"(cap {SURVEY_MAX_CHUNKS}, ~{CHUNK_CHARS} chars each)")
            ids = [c["id"] for c in chunks if c["id"]]
            if ids:
                print(f"Requirement id: {', '.join(ids[:12])}"
                      f"{' …' if len(ids) > 12 else ''}")
            else:
                print("Requirement id: none found — chunked by heading or paragraph")
            print()

    print(f"Surveying {url} … (one model call per chunk; this can take minutes)")
    print()

    def progress(done, total, label):
        print(f"  [{done}/{total}] {label}", flush=True)

    started = time.time()
    driver = _make_driver()
    try:
        driver.get(url)
        result = fast_survey(
            driver=driver,
            url=url,
            provider=get_provider(),
            document_context=document_context,
            on_progress=progress,
        )
    finally:
        driver.quit()

    cases = result.get("test_cases", [])
    report = result.get("requirement_report") or {}

    # Group by scenario, which is the feature-area level above a test case.
    by_scenario: dict = {}
    for c in cases:
        by_scenario.setdefault(c.get("scenario") or "General", []).append(c)

    print()
    print("=" * 72)
    print(f"{len(cases)} TEST CASE(S) IN {len(by_scenario)} SCENARIO(S) "
          f"— {time.time() - started:.0f}s")
    print("=" * 72)

    stepped = 0
    for scenario, items in by_scenario.items():
        req = next((i.get("requirement_id") for i in items if i.get("requirement_id")), None)
        print()
        print(f"SCENARIO: {scenario}" + (f"   [{req}]" if req else ""))
        for c in items:
            steps = c.get("steps") or []
            if steps:
                stepped += 1
            bits = [b for b in (c.get("priority"), c.get("category")) if b]
            print(f"  • {c['title']}" + (f"   ({', '.join(bits)})" if bits else ""))
            if not steps:
                print("      (no steps — one-line test case)")
            for s in steps:
                flag = "" if s.get("matched", True) else "   << NO MATCHING UI ELEMENT"
                hint = s.get("element_hint")
                data_input = s.get("data_input")
                wait = s.get("wait_condition") or "none"
                print(f"      {s['n']}. {s['step']}{flag}")
                print(f"         -> {s.get('expected') or '(no expected result)'}")
                print(f"         [hint: {hint if hint else '(none)'}  "
                      f"data: {data_input or 'none'}  wait: {wait}]")

    print()
    print("-" * 72)
    print(f"test cases with steps : {stepped} of {len(cases)}")
    unmatched = [(c["title"], s["step"]) for c in cases
                 for s in (c.get("steps") or []) if not s.get("matched", True)]
    print(f"steps with no element : {len(unmatched)}")
    for title, step in unmatched[:10]:
        print(f"    {title[:40]:42} {step[:45]}")

    if report:
        print()
        print("REQUIREMENT COVERAGE")
        print(f"  surveyed     : {len(report.get('requirements') or [])}")
        zero = report.get("zero_coverage") or []
        print(f"  produced none: {len(zero)}" + (f"  {', '.join(zero[:10])}" if zero else ""))
        never = report.get("not_surveyed") or []
        print(f"  not surveyed : {len(never)}" + (f"  {', '.join(never[:10])}" if never else ""))
        if report.get("truncated"):
            print(f"  ** hit SURVEY_MAX_CHUNKS={SURVEY_MAX_CHUNKS}; raise it to cover the rest")

    print()
    print("Nothing was saved. Accept via the UI to put any of this in the repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
