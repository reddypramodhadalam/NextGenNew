"""
api/routes/app_credentials.py

Per-user application login credentials.

Every Kavach user who works on an application stores THEIR OWN login for that
application (so a crawl/generation/execution they trigger signs in as them,
not as a shared account). These endpoints only ever read or write the
CALLER'S credential:

    GET    /v1/app-credentials/{application_id}          — the caller's saved
                                                            username + whether a
                                                            password is stored
                                                            (password masked)
    PUT    /v1/app-credentials/{application_id}          — save/replace the
                                                            caller's credential
    POST   /v1/app-credentials/{application_id}/reveal   — reveal the caller's
                                                            own password
    DELETE /v1/app-credentials/{application_id}          — delete the caller's
                                                            credential

ISOLATION
    The owning user is NEVER taken from the request body — it is resolved
    server-side from the caller's identity (auth/identity.resolve_user_id) and
    every query goes through the single door
    core.db.queries.app_credentials.get_credential(application_id, user_id),
    which always filters by BOTH keys. There is no endpoint that lists all
    users' credentials for an application and none that accepts an arbitrary
    user_id. So one user can never see or change another user's credential
    through this router.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth.identity import resolve_user_id
from core.db.queries import app_credentials as creds_db

logger = logging.getLogger(__name__)
router = APIRouter()

# Mirror the mask the test-data route uses, so the UI treats "a stored secret"
# the same way everywhere and never has to special-case this screen.
MASKED_SECRET = "********"


class CredentialBody(BaseModel):
    username: str
    # When the password comes back as the mask (the user edited only the
    # username and never retyped the secret), we keep the stored password
    # instead of overwriting it with "********".
    password: str


def _status_payload(application_id: int, user_id: int) -> dict:
    """The non-secret view of the caller's credential for this application."""
    stored = creds_db.get_credential(application_id, user_id)
    if stored is None:
        return {
            "application_id": application_id,
            "username": "",
            "has_password": False,
            "password": "",
        }
    return {
        "application_id": application_id,
        "username": stored["username"],
        "has_password": bool(stored["password"]),
        # Never leak the real password on a plain GET — only the reveal
        # endpoint returns plaintext, and only for the caller themselves.
        "password": MASKED_SECRET if stored["password"] else "",
    }


@router.get("/app-credentials/{application_id}")
def get_app_credential(application_id: int, request: Request):
    """Return the caller's own saved credential (password masked)."""
    user_id = resolve_user_id(request)
    try:
        return _status_payload(application_id, user_id)
    except RuntimeError as e:
        # Encryption key missing/rotated — surface clearly rather than 500.
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/app-credentials/{application_id}")
def save_app_credential(application_id: int, body: CredentialBody, request: Request):
    """Create or replace the caller's credential for this application."""
    user_id = resolve_user_id(request)

    username = (body.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")

    password = body.password or ""
    if not password or password == MASKED_SECRET:
        # The user left the password untouched. Keep whatever is already
        # stored; only update the username.
        existing = creds_db.get_credential(application_id, user_id)
        if existing is None or not existing["password"]:
            raise HTTPException(
                status_code=400,
                detail="A password is required the first time you save this credential.",
            )
        password = existing["password"]

    try:
        creds_db.upsert_credential(application_id, user_id, username, password)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _status_payload(application_id, user_id)


class RevealResponse(BaseModel):
    application_id: int
    username: str
    password: str


@router.post("/app-credentials/{application_id}/reveal")
def reveal_app_credential(application_id: int, request: Request):
    """Return the caller's OWN password in plaintext (deliberate reveal)."""
    user_id = resolve_user_id(request)
    try:
        stored = creds_db.get_credential(application_id, user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="You have not saved a credential for this application.",
        )
    return {
        "application_id": application_id,
        "username": stored["username"],
        "password": stored["password"],
    }


@router.delete("/app-credentials/{application_id}")
def delete_app_credential(application_id: int, request: Request):
    """Delete the caller's own credential for this application."""
    user_id = resolve_user_id(request)
    removed = creds_db.delete_credential(application_id, user_id)
    return {"application_id": application_id, "deleted": removed}
