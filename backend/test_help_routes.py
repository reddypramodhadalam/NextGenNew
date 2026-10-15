"""
test_help_routes.py

End-to-end gate test for api/routes/help.py.

Exercises the API over HTTP against a running backend, so it catches what a
unit test cannot: route ordering, request parsing, response shapes, and the
multipart upload path.

Start the backend first, then from the backend directory:

    python test_help_routes.py

Uses urllib rather than requests, so it needs nothing installed, and reads the
port from settings rather than assuming one.

It creates a temporary article and uploads a small temporary training
document, then removes both. If it is interrupted mid-run, rerunning cleans up
the leftovers first.

What it checks:
    1. Every literal route resolves — no 405 from a path being swallowed by
       '/help/articles/{slug}'. This is the failure mode this codebase has hit
       before, and it only shows up over HTTP.
    2. Search returns results and flags a gap when it finds nothing.
    3. Fetching an article records a view and returns its id.
    4. Acknowledgement respects the X-Triggered-By identity.
    5. Approval moves a draft into search results.
    6. Upload stores, extracts, indexes and lists a training document.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from core.db.connection import get_conn, init_pool
from core.settings import settings
from service import help_service


MARKER = "zz-help-routes-selftest"
USER = "selftest-routes"
BASE = f"http://127.0.0.1:{settings.app.port}/v1"

BODY = """Kavach schedules run test suites without anyone starting them.

## Creating a schedule

Choose the suite, the environment and the frequency. A schedule set to once
runs a single time and then stops.

## Pausing a schedule

Switch it off and it stays in place with all its settings, but does not fire
again until switched back on.
"""


def _request(method: str, path: str, body=None, who: str = USER):
    """Send one HTTP request and decode the response.

    Args:
        method: HTTP method.
        path: Path below /v1, beginning with '/'.
        body: JSON-serialisable body, or None.
        who: Value for the X-Triggered-By header.

    Returns:
        (status_code, decoded_body). The body is parsed JSON where possible,
        otherwise raw text.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, method=method, data=data,
        headers={"X-Triggered-By": who,
                 **({"Content-Type": "application/json"} if data else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
            try:
                return r.status, json.loads(text)
            except json.JSONDecodeError:
                return r.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text
    except urllib.error.URLError as e:
        print(f"\n  Cannot reach the backend at {BASE} — is it running?\n  {e.reason}")
        sys.exit(2)


def _upload(path: str, filename: str, content: bytes):
    """Send a multipart file upload.

    Built by hand rather than with a library, so the test adds no dependency.

    Args:
        path: Path below /v1.
        filename: Name to send the file under.
        content: File bytes.

    Returns:
        (status_code, decoded_body).
    """
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: text/plain\r\n\r\n",
        content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        BASE + path, method="POST", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-Triggered-By": USER},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


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


def _cleanup() -> None:
    """Remove every row and file the test created."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM help_views WHERE article_id IN "
                "(SELECT id FROM help_articles WHERE slug LIKE %s)", (f"{MARKER}%",))
            cur.execute("SELECT id, storage_path FROM documents "
                        "WHERE document_type = 'training' AND original_filename LIKE %s",
                        (f"{MARKER}%",))
            for doc_id, storage_path in cur.fetchall():
                cur.execute("DELETE FROM help_views WHERE document_id = %s", (doc_id,))
                cur.execute("DELETE FROM help_chunks WHERE document_id = %s", (doc_id,))
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
                try:
                    Path(storage_path).unlink(missing_ok=True)
                except OSError:
                    pass
            cur.execute("UPDATE help_articles SET superseded_by = NULL WHERE slug LIKE %s",
                        (f"{MARKER}%",))
            cur.execute("DELETE FROM help_articles WHERE slug LIKE %s", (f"{MARKER}%",))
            cur.execute("DELETE FROM help_search_misses WHERE asked_by = %s", (USER,))


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    init_pool()
    _cleanup()  # clear anything left by an interrupted previous run

    failures: list[str] = []
    slug = f"{MARKER}-schedules"

    try:
        # Seed one draft article directly; authoring is not an API concern yet.
        article = help_service.upsert_article(
            slug=slug, title="Scheduling a run", category=f"{MARKER}-category",
            body_md=BODY, summary="Running suites unattended",
            author=USER, status="draft", sort_order=10,
        )

        # 1. every literal route resolves
        print("\n  1. route resolution")
        for path in ("/help/categories", "/help/search?q=schedule", "/help/gaps",
                     "/help/records", "/help/training", "/help/articles"):
            status, _ = _request("GET", path)
            _check(failures, f"GET {path} -> {status}", status == 200, str(status))

        # 2. search
        print("\n  2. search")
        status, out = _request("GET", "/help/search?q=pausing%20a%20schedule&include_drafts=true")
        _check(failures, "finds the draft when drafts are included",
               status == 200 and any(r["label"] == slug for r in out["results"]),
               json.dumps(out)[:200])

        status, out = _request("GET", "/help/search?q=pausing%20a%20schedule")
        _check(failures, "hides the draft from the default search",
               not any(r["label"] == slug for r in out["results"]))

        status, out = _request("GET", "/help/search?q=quarterly%20payroll%20reconciliation")
        _check(failures, "absent topic returns nothing", out["results"] == [])
        _check(failures, "absent topic is flagged as a gap", out["recorded_as_gap"] is True)

        status, gaps = _request("GET", "/help/gaps")
        _check(failures, "the gap is listed", any("payroll" in g["query"] for g in gaps))

        # 3. fetching an article records a view
        print("\n  3. article and view")
        status, out = _request("GET", f"/help/articles/{slug}")
        _check(failures, "article resolves by slug", status == 200 and out.get("slug") == slug,
               json.dumps(out)[:200])
        _check(failures, "a view was recorded", bool(out.get("view_id")))
        view_id = out.get("view_id")

        status, out = _request("GET", f"/help/articles/{slug}?record_view=false")
        _check(failures, "preview does not record a view", out.get("view_id") is None)

        status, out = _request("GET", "/help/articles/no-such-article")
        _check(failures, "unknown slug returns an error, not a crash",
               status == 200 and "error" in out, str(status))

        # 4. acknowledgement respects identity
        print("\n  4. acknowledgement")
        status, out = _request("POST", f"/help/views/{view_id}/acknowledge",
                               who="someone-else")
        _check(failures, "refused for a different user", "error" in out, json.dumps(out)[:120])

        status, out = _request("POST", f"/help/views/{view_id}/acknowledge")
        _check(failures, "accepted for the original viewer", out.get("acknowledged") is True,
               json.dumps(out)[:120])

        status, recs = _request("GET", f"/help/records?viewed_by={USER}&acknowledged_only=true")
        _check(failures, "appears in the training record",
               any(r["id"] == view_id for r in recs))

        # 5. approval
        print("\n  5. approval")
        status, out = _request("POST", f"/help/articles/{article['id']}/approve")
        _check(failures, "approval records the approver", out.get("approved_by") == USER,
               json.dumps(out)[:120])

        status, out = _request("GET", "/help/search?q=pausing%20a%20schedule")
        _check(failures, "approved article now appears in the default search",
               any(r["label"] == slug for r in out["results"]))

        # 6. training upload
        print("\n  6. training document upload")
        text = (
            "Kavach onboarding notes.\n\n"
            "Requirements, test cases and scripts are separated by a human "
            "approval at each step.\n\n"
            "Aardvark semaphore custard is a phrase placed here so a search "
            "for it matches this document and nothing else.\n"
        ).encode()
        status, out = _upload("/help/training", f"{MARKER}-onboarding.txt", text)
        _check(failures, "upload accepted", status == 200, str(out)[:200])
        _check(failures, "passages indexed", isinstance(out, dict) and out.get("chunks_indexed", 0) >= 1,
               json.dumps(out)[:200] if isinstance(out, dict) else str(out)[:200])
        _check(failures, "no extraction warning", isinstance(out, dict) and out.get("warning") is None)

        status, docs = _request("GET", "/help/training")
        mine = [d for d in docs if str(d["original_filename"]).startswith(MARKER)]
        _check(failures, "appears in the training list", bool(mine))
        _check(failures, "reports a passage count", bool(mine) and mine[0]["chunk_count"] >= 1)

        status, out = _request("GET", "/help/search?q=aardvark%20semaphore%20custard")
        _check(failures, "training document is searchable",
               any(r["source_type"] == "training_doc" for r in out["results"]),
               json.dumps(out)[:200])

        if mine:
            status, _ = _request("GET", f"/help/training/{mine[0]['id']}/file")
            _check(failures, "training document downloads", status == 200, str(status))

    finally:
        _cleanup()
        print("\n  Cleaned up temporary rows and files.")

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
