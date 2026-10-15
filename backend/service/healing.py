"""
service/healing.py

Self-healing for broken selectors.

When a UI changes — a class is renamed, an aria-label reworded, an element
restructured — the application still works but the test can no longer find
what it is looking for. The test fails for a reason that has nothing to do
with a defect. This is the single most common cause of false failures in UI
automation, and the reason teams stop trusting their suites.

WHAT THIS DOES
    1. Reads the failing script and the real pytest error.
    2. Opens the live page and extracts what is actually there now.
    3. Asks the model, in ONE call, which selector broke and what it should
       be, grounded in the elements that genuinely exist.
    4. Records a PROPOSAL — old selector, new selector, confidence,
       reasoning — and stops.

WHAT IT DELIBERATELY DOES NOT DO
    It never edits the script on its own. A human approves or rejects, and
    the decision is recorded with who made it and when. Silently rewriting
    tests would mean a test could drift from what it was validated to check,
    which is unacceptable in a regulated environment — and it would also
    hide real defects behind "healed" selectors.
"""

import json
import logging
import re
from typing import Optional

from core.db.connection import get_conn
from llm.provider_factory import get_provider
from service.fast_generation import extract_multi_state, _strip_fences

logger = logging.getLogger(__name__)


DIAGNOSE_PROMPT = """A Selenium test has started failing. The application still works — most
likely a selector no longer matches the page.

THE SCRIPT:
__SCRIPT__

THE FAILURE:
__ERROR__

WHAT IS ON THE PAGE NOW (grouped by state; anything under "after clicking X"
only exists once X has been clicked):
__ELEMENTS__

Almost always the cause is that an element was renamed or restructured, and
the fix is a new selector. Read the error message to tell which case you are
in:

  NoSuchElementException, TimeoutException, or "unable to locate element"
      The selector matches nothing. The element it was looking for has been
      renamed or changed. FIND the element in the list above that it was
      clearly meant to match — same role, same position, similar wording —
      and propose a selector for it built from the attributes that element
      actually has. This is the common case and what healing is for.

  "is not displayed", or an assertion failing on is_displayed()
      The element WAS found, so the selector is fine. It is present but not
      visible. Check the list above: if that element appears under an
      "after clicking X" state, the script never clicked X. Set
      new_selector to null and name X in the reasoning.

Rules for the replacement:
- Use ONLY elements that appear in the list above. Never invent one.
- Build the selector from the attribute the element really has. An element
  whose PLACEHOLDER is "Search" needs [placeholder='Search'], not
  [aria-label='Search'].
- old_selector must be copied EXACTLY as it appears in the script, quotes
  included, so it can be found and replaced.
- Never return a new_selector identical to old_selector.
- Prefer stable attributes: id, then data-testid, then aria-label, then
  visible text. Avoid generated CSS class names — they change on rebuild.
- Matching by visible text requires XPath; CSS cannot do it and
  :contains() is not valid CSS.
- strategy is one of: id, css, xpath.
- confidence: "high" if one element clearly corresponds to the old one,
  "medium" if a reasonable match exists, "low" if you are guessing.
- Only if NOTHING in the list could replace it, set new_selector to null and
  explain why — the element may genuinely have been removed, which is a real
  defect rather than a broken selector.

Return ONLY this JSON, no prose, no markdown fences:
{"old_selector": "...", "new_selector": "...", "strategy": "xpath",
 "confidence": "high", "reasoning": "one or two sentences"}
"""


def diagnose_failure(generation_id: int, execution_id: Optional[int] = None) -> dict:
    """
    Diagnose why a test failed and propose a selector fix.

    Args:
        generation_id: The script that failed.
        execution_id: A specific failed run; defaults to the most recent.

    Returns:
        dict with the proposal, or {"error": "..."} if it could not diagnose.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT g.script_content, g.script_path, a.base_url, p.title
                   FROM generations g
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   JOIN applications a ON a.id = p.application_id
                   WHERE g.id = %s""",
                (generation_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Script not found."}
            script_content, script_path, base_url, title = row

            if execution_id:
                cur.execute(
                    "SELECT id, output FROM executions WHERE id = %s", (execution_id,)
                )
            else:
                cur.execute(
                    """SELECT id, output FROM executions
                       WHERE generation_id = %s AND result IN ('fail','error')
                       ORDER BY executed_at DESC LIMIT 1""",
                    (generation_id,),
                )
            exec_row = cur.fetchone()

    if not exec_row:
        return {"error": "No failed run to diagnose. Run the test first."}
    execution_id, error_output = exec_row[0], (exec_row[1] or "")

    # Read the script from disk if possible — it is what actually ran.
    if script_path:
        try:
            from pathlib import Path
            if Path(script_path).exists():
                script_content = Path(script_path).read_text(encoding="utf-8")
        except OSError:
            pass

    if not script_content:
        return {"error": "No script content to diagnose."}

    # Look at the page as it is NOW, not as it was when the test was written.
    driver = None
    try:
        from service.fast_generation import _get_map
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1400,1000")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(options=opts)
        elements = extract_multi_state(driver, base_url)
    except Exception as e:
        logger.exception("Could not inspect the page for healing")
        return {"error": f"Could not open {base_url}: {e}"}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    prompt = (
        DIAGNOSE_PROMPT
        .replace("__SCRIPT__", script_content[:6000])
        .replace("__ERROR__", error_output[-2500:])
        .replace("__ELEMENTS__", json.dumps(elements, indent=1))
    )

    provider = get_provider()
    tool = provider.build_tool([])
    contents = [provider.user_text_content(prompt)]
    response = provider.call_model("You reply with JSON only.", tool, contents)
    text = " ".join(
        p.text for p in response.candidates[0].content.parts if getattr(p, "text", "")
    ).strip()

    proposal = _parse_diagnosis(text)
    if not proposal:
        return {"error": "Could not interpret the diagnosis.", "raw": text[:400]}

    if not proposal.get("old_selector"):
        return {"error": "Could not identify which selector broke.", **proposal}

    # A "fix" identical to the original is not a fix. It usually means the
    # selector was fine and the test opened the wrong menu, so say that
    # instead of offering a no-op change.
    old_s = (proposal.get("old_selector") or "").strip()
    new_s = (proposal.get("new_selector") or "").strip()
    if new_s and old_s == new_s:
        logger.info("Diagnosis returned an identical selector — treating as 'not a selector problem'")
        proposal["new_selector"] = None
        proposal["confidence"] = "high"
        proposal["reasoning"] = (
            "No selector change is needed — the same selector was proposed as a "
            "replacement for itself. "
            + (proposal.get("reasoning") or "")
        ).strip()

    # A replacement identical to the original changes nothing. It means the
    # selector was never the problem — the script is not opening the menu that
    # makes the element visible. Say that instead of offering a no-op fix.
    old_sel = (proposal.get("old_selector") or "").strip()
    new_sel = (proposal.get("new_selector") or "").strip()
    if new_sel and old_sel == new_sel:
        logger.info("Diagnosis returned an identical selector — not a healing case")
        proposal["new_selector"] = None
        proposal["confidence"] = "high"
        proposal["reasoning"] = (
            "The selector is correct — the element exists on the page. It is not "
            "visible because the script never opened the menu containing it, so "
            "this is a problem with the test's steps rather than its selector. "
            "Healing cannot fix it: regenerate the script, or edit it to click "
            "the control that reveals this element first. "
            + (proposal.get("reasoning") or "")
        )

    # The old selector must genuinely appear in the script, or the fix cannot
    # be applied later.
    if proposal["old_selector"] not in script_content:
        logger.warning("Proposed old_selector not found verbatim in the script: %r",
                       proposal["old_selector"][:80])
        proposal["confidence"] = "low"
        proposal["reasoning"] = (
            "The selector named could not be found verbatim in the script, so "
            "this needs checking by hand. " + proposal.get("reasoning", "")
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO healing_proposals
                   (execution_id, old_selector, proposed_role, proposed_name,
                    confidence, reasoning, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                   RETURNING id, proposed_at""",
                (
                    execution_id,
                    (proposal.get("old_selector") or "")[:500],
                    (proposal.get("strategy") or "css")[:50],
                    (proposal.get("new_selector") or "")[:500],
                    proposal.get("confidence", "low"),
                    (proposal.get("reasoning") or "")[:2000],
                ),
            )
            healing_id, proposed_at = cur.fetchone()
        conn.commit()

    logger.info("Healing proposal %s for generation %s: %r -> %r (%s)",
                healing_id, generation_id, proposal["old_selector"][:50],
                (proposal.get("new_selector") or "")[:50], proposal.get("confidence"))

    return {
        "healing_id": healing_id,
        "generation_id": generation_id,
        "execution_id": execution_id,
        "test_title": title,
        "old_selector": proposal["old_selector"],
        "new_selector": proposal.get("new_selector"),
        "strategy": proposal.get("strategy", "css"),
        "confidence": proposal.get("confidence", "low"),
        "reasoning": proposal.get("reasoning", ""),
        "status": "pending",
        "proposed_at": str(proposed_at),
    }


def apply_healing(healing_id: int, decided_by: str = "anonymous") -> dict:
    """
    Apply an approved fix to the script.

    Only ever called from an explicit human approval. The old selector is
    replaced with the new one in both the file on disk and the database copy,
    and the decision is recorded against the proposal.

    Args:
        healing_id: The proposal being approved.
        decided_by: Who approved it — stored for the audit trail.

    Returns:
        dict describing what was changed.
    """
    from pathlib import Path

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.old_selector, h.proposed_name, h.status,
                          g.id, g.script_path, g.script_content
                   FROM healing_proposals h
                   JOIN executions e ON e.id = h.execution_id
                   JOIN generations g ON g.id = e.generation_id
                   WHERE h.id = %s""",
                (healing_id,),
            )
            row = cur.fetchone()

    if not row:
        return {"error": "Healing proposal not found."}
    old_sel, new_sel, status, generation_id, script_path, script_content = row

    if status != "pending":
        return {"error": f"This proposal was already {status}."}
    if not new_sel:
        return {"error": "No replacement selector was proposed, so there is nothing to apply."}

    if script_path and Path(script_path).exists():
        try:
            script_content = Path(script_path).read_text(encoding="utf-8")
        except OSError as e:
            return {"error": f"Could not read the script: {e}"}

    if old_sel not in script_content:
        return {"error": "The original selector is no longer present in the script; "
                         "it may already have been changed."}

    updated = script_content.replace(old_sel, new_sel)
    occurrences = script_content.count(old_sel)

    if script_path:
        try:
            Path(script_path).write_text(updated, encoding="utf-8")
        except OSError as e:
            return {"error": f"Could not write the script: {e}"}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE generations SET script_content = %s, is_manually_edited = TRUE, "
                "edited_at = now() WHERE id = %s",
                (updated, generation_id),
            )
            cur.execute(
                "UPDATE healing_proposals SET status = 'approved', decided_at = now(), "
                "decided_by = %s WHERE id = %s",
                (decided_by, healing_id),
            )
        conn.commit()

    logger.info("Healing %s approved by %s: replaced %d occurrence(s) in generation %s",
                healing_id, decided_by, occurrences, generation_id)
    return {
        "applied": True,
        "healing_id": healing_id,
        "generation_id": generation_id,
        "replacements": occurrences,
        "old_selector": old_sel,
        "new_selector": new_sel,
    }


def reject_healing(healing_id: int, decided_by: str = "anonymous") -> dict:
    """Record that a reviewer rejected the proposed fix."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE healing_proposals SET status = 'rejected', decided_at = now(), "
                "decided_by = %s WHERE id = %s AND status = 'pending'",
                (decided_by, healing_id),
            )
            changed = cur.rowcount
        conn.commit()
    if not changed:
        return {"error": "Proposal not found, or it was already decided."}
    logger.info("Healing %s rejected by %s", healing_id, decided_by)
    return {"rejected": True, "healing_id": healing_id}


def list_for_generation(generation_id: int) -> list[dict]:
    """Return the healing history for a script, newest first."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.id, h.old_selector, h.proposed_role, h.proposed_name,
                          h.confidence, h.reasoning, h.status, h.proposed_at,
                          h.decided_at, h.decided_by, h.execution_id
                   FROM healing_proposals h
                   JOIN executions e ON e.id = h.execution_id
                   WHERE e.generation_id = %s
                   ORDER BY h.proposed_at DESC""",
                (generation_id,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _parse_diagnosis(text: str) -> Optional[dict]:
    """Parse the model's JSON diagnosis, tolerating fences or surrounding prose."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None