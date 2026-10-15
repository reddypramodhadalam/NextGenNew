"""
api/routes/imports.py

Import manual test cases from a spreadsheet.

  POST /v1/applications/{id}/import-preview   upload, detect, parse — saves nothing
  (accepting is the existing /v1/test-suites/accept-proposals)

Preview and accept are separate on purpose. The importer guesses what the
columns mean, and a wrong guess would otherwise put hundreds of malformed
rows into the repository before anyone noticed. So it shows what it worked
out, you confirm, and only then does the same approval gate that AI proposals
go through actually write anything.
"""

import asyncio
import logging
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile

from service import test_case_import as importer
from api.routes.workflows import WorkflowRoute
from service.workflow_control import checkpoint, current_operation

logger = logging.getLogger(__name__)
router = APIRouter(route_class=WorkflowRoute)

ALLOWED = (".xlsx", ".xlsm", ".csv")



def _existing_titles(application_id: int) -> dict:
    """
    Titles already in this application, so import can flag duplicates.

    Also reports whether each has a script — a duplicate with a broken
    script is exactly the one worth replacing, and the reviewer should be
    able to see that.
    """
    from core.db.connection import get_conn

    out = {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT p.id, p.title,
                              EXISTS (SELECT 1 FROM generations g
                                       WHERE g.test_case_proposal_id = p.id
                                         AND g.script_path IS NOT NULL
                                         AND g.is_stale = FALSE) AS has_script
                       FROM test_case_proposals p
                       WHERE p.application_id = %s AND p.status != 'archived'""",
                    (application_id,),
                )
                for pid, title, has_script in cur.fetchall():
                    out[(title or "").strip().lower()] = {
                        "id": pid, "title": title, "has_script": has_script,
                    }
    except Exception as e:
        logger.warning("Could not load existing titles for duplicate check: %s", e)
    return out


@router.post("/applications/{application_id}/import-preview")
async def import_preview(
    application_id: int,
    file: UploadFile = File(...),
    sheet: Optional[str] = Form(None),
    title_column: Optional[str] = Form(None),
    step_column: Optional[str] = Form(None),
    expected_column: Optional[str] = Form(None),
    row_mode: Optional[str] = Form(None),
):
    """
    Read a spreadsheet and return what it contains — without saving anything.

    Called twice in practice: once to see the detected mapping, and again
    with corrected columns if the detection was wrong. Nothing is written
    either time.

    Args:
        application_id: Which application these test cases belong to.
        file: .xlsx, .xlsm or .csv.
        sheet: Optional sheet name.
        title_column / step_column / expected_column / row_mode: supplied on
            the second call to override what was detected.

    Returns:
        headers, the mapping used, the parsed test cases, and a sample of
        rows so the UI can show what it read.
    """
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED):
        return {"error": f"Unsupported file type. Use {', '.join(ALLOWED)}."}

    suffix = os.path.splitext(name)[1]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(await file.read())
        tmp.close()

        if current_operation():
            headers, rows = await asyncio.to_thread(importer.read_sheet, tmp.name, sheet)
        else:
            headers, rows = importer.read_sheet(tmp.name, sheet)
        checkpoint()
        if not headers:
            return {"error": "That file appears to be empty."}

        if title_column:
            # The reviewer corrected the mapping — use theirs, do not detect again.
            mapping = {
                "title_column": title_column,
                "step_column": step_column,
                "expected_column": expected_column,
                "priority_column": None, "requirement_column": None, "module_column": None,
                "row_mode": row_mode or "one_row_per_case",
                "blank_title_continues": (row_mode == "one_row_per_step"),
                "confidence": "high",
                "reasoning": "Set by hand.",
                "source": "set by hand",
            }
        else:
            provider = None
            try:
                from llm.provider_factory import get_provider
                provider = get_provider()
            except Exception as e:
                logger.warning("No model available for column detection: %s", e)
            if current_operation():
                mapping = await asyncio.to_thread(importer.detect_mapping, headers, rows, provider)
            else:
                mapping = importer.detect_mapping(headers, rows, provider)

        checkpoint()
        cases = importer.parse_test_cases(headers, rows, mapping)

        # Flag anything that already exists so the reviewer can decide
        # whether to skip it or replace it — the same choice the survey
        # offers. Deciding silently either way would be wrong: a duplicate
        # might be the one with a broken script.
        existing = _existing_titles(application_id)
        for p_ in cases:
            prior = existing.get(p_["title"].strip().lower())
            p_["duplicate_of"] = prior["id"] if prior else None
            p_["duplicate_title"] = prior["title"] if prior else None
            p_["duplicate_has_script"] = bool(prior and prior["has_script"])
        logger.info("Import preview: %d row(s) -> %d test case(s) from %s",
                    len(rows), len(cases), file.filename)

        return {
            "filename": file.filename,
            "headers": headers,
            "row_count": len(rows),
            "mapping": mapping,
            "proposals": cases,
            "sample_rows": rows[:8],
        }
    except Exception as e:
        logger.exception("Import preview failed")
        return {"error": f"Could not read that file: {e}"}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
