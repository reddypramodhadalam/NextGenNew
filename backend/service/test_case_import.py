"""
service/test_case_import.py

Import manual test cases from a spreadsheet.

The point is that teams should NOT have to reformat their files. Every team
writes test cases differently — "Test Case Description" here, "Scenario"
there, steps in one cell in one file and one row per step in another. Asking
people to normalise hundreds of test cases before they can use the tool is
how a tool goes unused.

So the shape is detected rather than demanded:

  1. read the headers and a sample of rows
  2. ask the model what each column means, and whether one row is a whole
     test case or a single step
  3. show that mapping for confirmation before anything is imported
  4. parse with it, and hand the result to the SAME review-and-accept gate
     that AI-proposed scenarios go through

A heuristic fallback covers the common header names, so import never depends
on the model being available — it just works better when it is.
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Header words that reliably indicate a column's meaning. Used when the model
# is unavailable, and to sanity-check what it returns.
HEURISTICS = {
    "title": ["test case", "testcase", "title", "name", "scenario", "summary",
              "description", "test name", "objective"],
    "step": ["step", "action", "procedure", "instruction", "test step", "steps"],
    "expected": ["expected", "result", "outcome", "verification", "acceptance",
                 "expected result", "expected outcome"],
    "priority": ["priority", "severity", "importance"],
    "requirement": ["requirement", "req", "story", "ticket", "user story", "rtm"],
    "module": ["module", "feature", "area", "component", "epic", "suite"],
}


DETECT_PROMPT = """This spreadsheet holds manual test cases. Work out what each
column means and how the rows are organised.

COLUMNS:
__HEADERS__

FIRST ROWS (as they appear, tab-separated):
__SAMPLE__

Decide:
- which column holds the TEST CASE TITLE
- which holds the STEP or action
- which holds the EXPECTED RESULT
- which, if any, hold priority, requirement id, or module/feature
- row_mode:
    "one_row_per_case"  each row is a complete test case
    "one_row_per_step"  each row is a single step, and several rows together
                        make one test case
- if row_mode is "one_row_per_step", does a BLANK title mean "this row
  continues the test case above"?

Use the exact column names as given. Use null for anything not present.

Return ONLY this JSON, no prose, no markdown fences:
{"title_column": "...", "step_column": "...", "expected_column": "...",
 "priority_column": null, "requirement_column": null, "module_column": null,
 "row_mode": "one_row_per_step", "blank_title_continues": true,
 "confidence": "high", "reasoning": "one sentence"}
"""


def read_sheet(path: str, sheet: Optional[str] = None) -> tuple[list[str], list[list]]:
    """
    Read a spreadsheet into headers and rows.

    Args:
        path: .xlsx, .xlsm or .csv file.
        sheet: Sheet name; the first sheet is used if not given.

    Returns:
        (headers, rows) with values as strings, blanks as empty strings.
    """
    if path.lower().endswith(".csv"):
        import csv
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.reader(f)]
        if not rows:
            return [], []
        return [str(h or "").strip() for h in rows[0]], [
            [str(c or "").strip() for c in r] for r in rows[1:]
        ]

    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]

    all_rows = []
    for row in ws.iter_rows(values_only=True):
        all_rows.append([("" if c is None else str(c)).strip() for c in row])

    if not all_rows:
        return [], []

    # Exports often carry a title block or blank lines before the real header.
    # The header is the first row where most cells have text.
    header_idx = 0
    for i, row in enumerate(all_rows[:10]):
        filled = sum(1 for c in row if c)
        if filled >= max(2, len([c for c in row if c is not None]) // 2):
            header_idx = i
            break

    headers = all_rows[header_idx]
    rows = [r for r in all_rows[header_idx + 1:] if any(c for c in r)]
    return headers, rows


def detect_mapping(headers: list[str], rows: list[list], provider=None) -> dict:
    """
    Work out what the columns mean.

    Tries the model first because it reads headers the way a person does —
    "Verification Steps" and "Procedure" mean the same thing without anyone
    listing synonyms. Falls back to matching known header words so import
    still works when the model is unavailable.

    Returns:
        A mapping dict, always including 'source' so the UI can say how it
        was determined.
    """
    if provider is not None:
        try:
            sample = "\n".join("\t".join(r[:8]) for r in rows[:12])
            prompt = (DETECT_PROMPT
                      .replace("__HEADERS__", "\n".join(f"- {h}" for h in headers if h))
                      .replace("__SAMPLE__", sample[:3000]))
            tool = provider.build_tool([])
            resp = provider.call_model("You reply with JSON only.", tool,
                                       [provider.user_text_content(prompt)])
            text = " ".join(p.text for p in resp.candidates[0].content.parts
                            if getattr(p, "text", "")).strip()
            mapping = _parse_json(text)
            if mapping and mapping.get("title_column") in headers:
                mapping["source"] = "detected by model"
                logger.info("Import mapping detected: %s", mapping)
                return mapping
            logger.warning("Model mapping unusable (%s); falling back to headers", mapping)
        except Exception as e:
            logger.warning("Column detection failed, falling back to headers: %s", e)

    return _heuristic_mapping(headers, rows)


def _heuristic_mapping(headers: list[str], rows: list[list]) -> dict:
    """Match column names against known header words."""
    # An ID column is not a title. "Test Case ID" and "Test Case Description"
    # both contain "test case", and matching the first one gives every test
    # a name like "TC-001", which tells a reader nothing.
    def is_identifier(h):
        hl = h.lower().strip()
        return hl.endswith(" id") or hl in ("id", "tc id", "case id", "no", "no.", "s.no", "sr no")

    found = {}
    for field, words in HEURISTICS.items():
        exact, partial = None, None
        for i, h in enumerate(headers):
            hl = (h or "").lower().strip()
            if not hl:
                continue
            if field == "title" and is_identifier(h):
                continue
            if any(w == hl for w in words):
                exact = headers[i]
                break
            if partial is None and any(w in hl for w in words):
                partial = headers[i]
        if exact or partial:
            found[field] = exact or partial

    # If titles repeat or are blank on most rows, each row is a step rather
    # than a whole test case.
    row_mode = "one_row_per_case"
    title_col = found.get("title")
    if title_col and title_col in headers:
        idx = headers.index(title_col)
        vals = [r[idx] if idx < len(r) else "" for r in rows]
        blanks = sum(1 for v in vals if not v)
        if vals and blanks / len(vals) > 0.3:
            row_mode = "one_row_per_step"

    return {
        "title_column": found.get("title"),
        "step_column": found.get("step"),
        "expected_column": found.get("expected"),
        "priority_column": found.get("priority"),
        "requirement_column": found.get("requirement"),
        "module_column": found.get("module"),
        "row_mode": row_mode,
        "blank_title_continues": row_mode == "one_row_per_step",
        "confidence": "medium" if found.get("title") else "low",
        "reasoning": "Matched on column names.",
        "source": "matched on column names",
    }


def flatten_steps(steps: Optional[list]) -> str:
    """
    Render structured steps as the numbered text the script generator reads.

    Base format (always present):
        1. Click the Settings button  →  The settings menu opens

    Phase 3-C: Element hints (when available):
        2. Click Settings [aria-label='Settings']  →  Settings menu opens
        3. Enter username [name='username']  →  Field accepts input

    Phase 3-A: Unautomatable step marking:
        4. [NOT AUTOMATED] Verify receipt (element not found)  →  Confirmation visible

    Extended format (when data_input / wait_condition are present):
        5. Submit form [data: password]  →  Form submits  [wait: url_change]

    The annotations are plain-text suffixes so SCRIPT_PROMPT can parse them
    without any schema change.  Both the spreadsheet importer and the BRD
    survey call this function — keeping one formatting path guarantees that
    imported and generated test cases produce identical scripts.

    Args:
        steps: List of step dicts with at minimum {"step": str, "expected": str}.
               Optional keys: "element_hint", "matched", "match_reason", "data_input",
               "wait_condition".
               - matched: bool (default True) — whether element was found during crawl
               - match_reason: str — explanation if matched=false
               - element_hint: str — selector hint like "aria-label='X'" (Phase 3-C)

    Returns:
        The flattened text, or "" if there are no steps (callers fall back
        to the title).
    """
    from service.step_validator import is_step_automatable, get_match_reason
    from service.hint_resolver import extract_hint, annotate_hint
    
    lines = []
    for i, s in enumerate(steps or [], 1):
        step_text = (s.get("step") or "").strip()
        expected = (s.get("expected") or "").strip()

        # Phase 3-A: Mark unautomatable steps (matched: false)
        automatable = is_step_automatable(s)
        
        if not automatable:
            reason = get_match_reason(s)
            line = f"{i}. [NOT AUTOMATED] {step_text}"
            if reason:
                line += f" ({reason})"
            logger.debug(
                "flatten_steps: step %d marked unautomatable — reason: %s",
                i, reason
            )
        else:
            # Phase 3-C: Include element hint if available
            hint = extract_hint(s)
            hint_text = annotate_hint(hint, matched=True) if hint else ""
            line = f"{i}. {step_text}{hint_text}" if step_text else f"{i}."
            
            if hint:
                logger.debug(
                    "flatten_steps: step %d includes element_hint=%s",
                    i, hint
                )

        # Append data_input annotation so the script generator knows which
        # environment key to use for send_keys — e.g. [data: username] → TEST_USERNAME.
        data_input = (s.get("data_input") or "").strip()
        if data_input:
            line += f" [data: {data_input}]"

        if expected:
            line += f"  \u2192  {expected}"

        # Append wait_condition annotation so the generator knows what to wait
        # for after this action before proceeding to the next step.
        wait_condition = (s.get("wait_condition") or "").strip()
        if wait_condition and wait_condition != "none":
            line += f"  [wait: {wait_condition}]"

        lines.append(line)
    
    result = "\n".join(lines)
    return result


def parse_test_cases(headers: list[str], rows: list[list], mapping: dict) -> list[dict]:
    """
    Turn rows into test cases using the mapping.

    Returns proposals in the same shape the survey produces, so imported
    cases go through the identical review-and-accept gate — an imported test
    is no more trustworthy than a generated one until a person accepts it.
    """
    def col(name):
        return headers.index(name) if name and name in headers else None

    ti, si, ei = col(mapping.get("title_column")), col(mapping.get("step_column")), col(mapping.get("expected_column"))
    pi, ri, mi = col(mapping.get("priority_column")), col(mapping.get("requirement_column")), col(mapping.get("module_column"))

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    cases: list[dict] = []
    current = None

    for row in rows:
        title = cell(row, ti)
        step = cell(row, si)
        expected = cell(row, ei)

        if mapping.get("row_mode") == "one_row_per_step":
            if title:                       # a title starts a new test case
                current = {
                    "title": title, "steps": [],
                    "priority": _norm_priority(cell(row, pi)),
                    "requirement_id": cell(row, ri) or None,
                    "module": cell(row, mi) or None,
                }
                cases.append(current)
            if current is not None and (step or expected):
                current["steps"].append({"step": step, "expected": expected})
        else:
            if not title and not step:
                continue
            cases.append({
                "title": title or step[:120],
                "steps": [{"step": step, "expected": expected}] if (step or expected) else [],
                "priority": _norm_priority(cell(row, pi)),
                "requirement_id": cell(row, ri) or None,
                "module": cell(row, mi) or None,
            })

    proposals = []
    for c in cases:
        if not c["title"]:
            continue
        # The steps become the description, because that is what generation
        # reads. Explicit steps are far better input than a one-line scenario:
        # they say what to do AND what should happen.
        proposals.append({
            "id": f"imported_{len(proposals) + 1}",
            "is_proposal": True,
            "title": c["title"][:200],
            "description": flatten_steps(c["steps"]) or c["title"],
            "step_count": len(c["steps"]),
            "priority": c["priority"],
            "category": None,
            "scenario": c["module"] or "Imported",
            "requirement_id": c["requirement_id"],
            "source": "bulk_import",
        })
    return proposals


def _norm_priority(value: str) -> Optional[str]:
    """Map whatever the sheet says onto P0-P3, or None."""
    if not value:
        return None
    v = value.strip().upper()
    if v in ("P0", "P1", "P2", "P3"):
        return v
    if v in ("CRITICAL", "BLOCKER", "HIGHEST"):
        return "P0"
    if v in ("HIGH", "MAJOR"):
        return "P1"
    if v in ("MEDIUM", "NORMAL", "MODERATE"):
        return "P2"
    if v in ("LOW", "MINOR", "TRIVIAL"):
        return "P3"
    return None


def _parse_json(text: str) -> Optional[dict]:
    """Parse the model's JSON, tolerating fences or surrounding prose."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
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
