"""
api/routes/test_data.py

Serve and edit externalised test-data files (the JSON under test_data/).

    GET   /v1/test-data/app                    — app-level data (optionally
                                                                                             scoped by application_id and
                                                                                             environment)
    PUT   /v1/test-data/app                    — save app-level data
    POST  /v1/test-data/app/reveal             — reveal one masked field
    GET   /v1/test-data/scenario/{filename}    — a scenario's test data
    PUT   /v1/test-data/scenario/{filename}    — save edits to a scenario file

For app-level data in scoped mode, password/secret-like fields are
encrypted at save, returned as masked values, and revealed only via the
explicit reveal endpoint.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
router = APIRouter()

TEST_DATA_DIR = Path(__file__).resolve().parents[2] / "test_data"
# Backward-compatibility for data written when the server cwd was repo root.
LEGACY_TEST_DATA_DIR = Path(__file__).resolve().parents[3] / "test_data"
SCENARIO_DIR = TEST_DATA_DIR / "scenarios"
APP_DIR = TEST_DATA_DIR / "applications"
DEFAULT_ENV = "default"
MASKED_SECRET = "********"

# Per-user ownership of app-level test data.
# The _created_by field stores the caller identity (from X-Triggered-By)
# that first saved the file. Subsequent reads/writes are restricted to that
# owner. Files without _created_by (pre-ownership data) remain visible to
# everyone until an edit stamps an owner on them.
OWNER_FIELD = "_created_by"
CREATED_AT_FIELD = "_created_at"


def _caller(request: Request) -> str:
    value = (request.headers.get("X-Triggered-By") or "").strip()
    return value or "anonymous"


def _is_owner(doc: dict, caller: str) -> bool:
    """True if the caller may see or edit this doc.

    Docs without an owner are shared (backward-compatible with data from
    before ownership was introduced). Docs with an owner are visible only
    to that owner.
    """
    owner = str(doc.get(OWNER_FIELD) or "").strip()
    if not owner:
        return True
    return owner.lower() == caller.lower()


class RevealFieldBody(BaseModel):
    application_id: int
    key: str
    environment: Optional[str] = None
    # When set, reveal environments.<env>.personas.<persona>.<key> instead
    # of a flat top-level field — e.g. persona="approver", key="password".
    # None (the default) is the existing flat-field behaviour, unchanged.
    persona: Optional[str] = None


class TestDataBody(BaseModel):
    content: dict


def _read(path: Path):
    if not path.exists():
        try:
            rel = path.relative_to(TEST_DATA_DIR)
            legacy = LEGACY_TEST_DATA_DIR / rel
            if legacy.exists():
                with open(legacy) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    with open(path) as f:
        return json.load(f)


def _write(path: Path, content: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(content, f, indent=2)


def _app_file(application_id: int) -> Path:
    return APP_DIR / f"app_{application_id}.json"


def _normalize_env(environment: Optional[str]) -> str:
    key = (environment or "").strip()
    return key if key else DEFAULT_ENV


def _load_app_doc(application_id: int) -> dict:
    path = _app_file(application_id)
    doc = _read(path)
    if not isinstance(doc, dict):
        return {"application_id": application_id, "environments": {}}
    doc.setdefault("application_id", application_id)
    envs = doc.get("environments")
    if not isinstance(envs, dict):
        doc["environments"] = {}
    return doc


def _save_app_doc(application_id: int, doc: dict) -> None:
    _write(_app_file(application_id), doc)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return "password" in k or "secret" in k


# Multi-actor "personas" block (environments.<env>.personas.<name>.
# {username, password}) — see test_data/loader.py's module docstring and
# service/grounded_runtime.py's _login_as for what these are used for.
# Every persona's "password" field is ALWAYS encrypted at save, the same
# way a flat top-level "password"/"secret" field already is — unlike the
# flat table, there is no opt-out checkbox for this: a password is a
# password regardless of where in the document it lives, so this is not
# gated behind __encrypt_keys at all.


def _fernet() -> Fernet:
    key = (os.environ.get("TEST_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        # Some runs start from the repo root rather than backend/, so
        # implicit dotenv discovery can miss backend/.env. Load it directly.
        backend_env = Path(__file__).resolve().parents[2] / ".env"
        if backend_env.exists():
            load_dotenv(dotenv_path=backend_env, override=False)
        key = (os.environ.get("TEST_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "TEST_DATA_ENCRYPTION_KEY is not set. Configure it before saving password/secret fields."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            "TEST_DATA_ENCRYPTION_KEY is invalid. It must be a valid Fernet key."
        ) from e


def _is_encrypted_payload(value) -> bool:
    return isinstance(value, dict) and value.get("_encrypted") is True and value.get("alg") == "fernet" and "value" in value


def _encrypt(plain: str) -> dict:
    token = _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    return {
        "_encrypted": True,
        "alg": "fernet",
        "value": token,
    }


def _decrypt(value) -> str:
    if not _is_encrypted_payload(value):
        return "" if value is None else str(value)
    try:
        raw = _fernet().decrypt(value["value"].encode("utf-8"))
        return raw.decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("Could not decrypt stored secret with current TEST_DATA_ENCRYPTION_KEY.") from e


def _persona_has_sensitive_fields(personas_payload) -> bool:
    """True if any persona in an incoming "personas" payload has a
    password/secret-named field — used only to decide, upfront, whether
    TEST_DATA_ENCRYPTION_KEY must be configured before attempting a save.
    """
    if not isinstance(personas_payload, dict):
        return False
    for creds in personas_payload.values():
        if isinstance(creds, dict) and any(_is_sensitive_key(str(f)) for f in creds):
            return True
    return False


def _encrypt_personas(incoming_personas, current_personas) -> dict:
    """
    Encrypt every persona's password/secret-named field the same way a
    flat top-level sensitive field is encrypted — see save_app_data's main
    loop, which this mirrors in miniature for the nested "personas" block.

    Unlike the flat table, this is unconditional: ANY field whose name
    matches _is_sensitive_key (chiefly "password") is always encrypted,
    with no __encrypt_keys opt-out — a persona's password must never be
    stored in plain text, irrespective of where it lives in the document.
    Non-sensitive fields (e.g. "username") are stored as given.

    Args:
        incoming_personas: The "personas" dict from the request body, or
            anything else (returns {} for non-dict input, so a malformed
            or absent block never raises — same defensive style as the
            rest of this file).
        current_personas: The previously saved "personas" dict for this
            environment, used only to preserve an existing encrypted
            password when the UI sends back the masked placeholder
            unchanged (same convention as the flat table's
            encryptedExisting / MASKED_SECRET round-trip).

    Returns:
        A new "personas" dict, ready to store — every sensitive field is
        either a fresh {_encrypted: true, ...} payload or the untouched
        previous one; every other field is passed through as given.
    """
    if not isinstance(incoming_personas, dict):
        return {}
    current_personas = current_personas if isinstance(current_personas, dict) else {}
    out = {}
    for persona_name, creds in incoming_personas.items():
        if not isinstance(creds, dict):
            continue
        existing_creds = current_personas.get(persona_name)
        existing_creds = existing_creds if isinstance(existing_creds, dict) else {}
        new_creds = {}
        for field, value in creds.items():
            if _is_sensitive_key(str(field)):
                if value == MASKED_SECRET and _is_encrypted_payload(existing_creds.get(field)):
                    # Unchanged in the UI — keep the existing encrypted
                    # payload rather than "encrypting" the mask token itself.
                    new_creds[field] = existing_creds[field]
                else:
                    try:
                        new_creds[field] = _encrypt("" if value is None else str(value))
                    except RuntimeError as e:
                        raise HTTPException(status_code=400, detail=str(e))
            else:
                new_creds[field] = value
        out[persona_name] = new_creds
    return out


def _masked_env_view(env_data: dict) -> dict:
    out = {}
    encrypted = []
    persona_encrypted = []
    for key, value in (env_data or {}).items():
        if key == "personas" and isinstance(value, dict):
            masked_personas = {}
            for persona_name, creds in value.items():
                if not isinstance(creds, dict):
                    masked_personas[persona_name] = creds
                    continue
                masked_creds = {}
                for field, field_value in creds.items():
                    if _is_encrypted_payload(field_value):
                        masked_creds[field] = MASKED_SECRET
                        persona_encrypted.append(f"{persona_name}.{field}")
                    else:
                        masked_creds[field] = field_value
                masked_personas[persona_name] = masked_creds
            out[key] = masked_personas
            continue
        if _is_encrypted_payload(value):
            out[key] = MASKED_SECRET
            encrypted.append(key)
        else:
            out[key] = value
    if encrypted:
        out["__encrypted_keys"] = encrypted
    if persona_encrypted:
        # Separate list, keyed "<persona>.<field>", so the UI can tell
        # which persona fields are currently masked without having to
        # special-case the shape of __encrypted_keys (which only ever
        # held flat top-level key names before personas existed).
        out["__encrypted_persona_keys"] = persona_encrypted
    return out


@router.get("/test-data/app")
def get_app_data(
    request: Request,
    application_id: Optional[int] = None,
    environment: Optional[str] = None,
):
    # Legacy fallback (existing callers without app/env scope).
    if application_id is None:
        return _read(TEST_DATA_DIR / "netra_app_data.json")

    doc = _load_app_doc(application_id)
    caller = _caller(request)
    if not _is_owner(doc, caller):
        # Not the owner — return empty rather than reveal that another
        # user has data for this application.
        return {}
    env_key = _normalize_env(environment)
    envs = doc.get("environments") or {}
    env_data = envs.get(env_key) or {}
    return _masked_env_view(env_data)


@router.put("/test-data/app")
def save_app_data(
    body: TestDataBody,
    request: Request,
    application_id: Optional[int] = None,
    environment: Optional[str] = None,
):
    # Legacy fallback (existing callers without app/env scope).
    if application_id is None:
        content = dict(body.content)
        content.pop("password", None)
        _write(TEST_DATA_DIR / "netra_app_data.json", content)
        return {"saved": True}

    incoming = dict(body.content or {})
    explicit_encrypt = incoming.get("__encrypt_keys")
    has_explicit_encrypt = isinstance(explicit_encrypt, list)
    explicit_encrypt_keys = {str(x) for x in explicit_encrypt} if has_explicit_encrypt else set()

    doc = _load_app_doc(application_id)
    caller = _caller(request)
    if not _is_owner(doc, caller):
        raise HTTPException(
            status_code=403,
            detail="This test data is owned by another user and cannot be modified.",
        )
    env_key = _normalize_env(environment)
    envs = doc.setdefault("environments", {})
    current = envs.get(env_key) if isinstance(envs.get(env_key), dict) else {}
    updated = {}

    sensitive_keys = [
        str(k)
        for k in incoming.keys()
        if not str(k).startswith("__")
        and str(k) != "personas"
        and (
            (has_explicit_encrypt and str(k) in explicit_encrypt_keys)
            or (not has_explicit_encrypt and _is_sensitive_key(str(k)))
        )
    ]
    persona_sensitive = _persona_has_sensitive_fields(incoming.get("personas"))
    if sensitive_keys or persona_sensitive:
        try:
            _fernet()
        except RuntimeError as e:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot save sensitive fields "
                    f"({', '.join(sensitive_keys) or 'persona passwords'}): {e}"
                ),
            )

    for key, value in incoming.items():
        if str(key).startswith("__"):
            continue
        if str(key) == "personas":
            # Nested identities for multi-actor procedures — see
            # _encrypt_personas's docstring. Every persona's password is
            # always encrypted; this never goes through the flat-field
            # should_encrypt/__encrypt_keys branch below, since a dict
            # value there would never match _is_sensitive_key anyway.
            updated["personas"] = _encrypt_personas(value, current.get("personas"))
            continue
        should_encrypt = (
            str(key) in explicit_encrypt_keys
            if has_explicit_encrypt
            else _is_sensitive_key(str(key))
        )

        if should_encrypt:
            # Preserve existing encrypted value when UI sends masked token.
            if value == MASKED_SECRET and _is_encrypted_payload(current.get(key)):
                updated[key] = current[key]
            else:
                try:
                    updated[key] = _encrypt("" if value is None else str(value))
                except RuntimeError as e:
                    raise HTTPException(status_code=400, detail=str(e))
        else:
            if value == MASKED_SECRET and _is_encrypted_payload(current.get(key)):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Field '{key}' is currently encrypted. Enter a plain value or mark it Encrypt before saving."
                    ),
                )
            updated[key] = value

    envs[env_key] = updated
    # Stamp ownership on first save. Keep whatever is already there on
    # subsequent saves so the original owner is preserved.
    if not doc.get(OWNER_FIELD):
        doc[OWNER_FIELD] = caller
        doc[CREATED_AT_FIELD] = datetime.now(timezone.utc).isoformat()
    _save_app_doc(application_id, doc)
    return {"saved": True}


@router.post("/test-data/app/reveal")
def reveal_app_data_field(body: RevealFieldBody, request: Request):
    doc = _load_app_doc(body.application_id)
    caller = _caller(request)
    if not _is_owner(doc, caller):
        raise HTTPException(
            status_code=403,
            detail="This test data is owned by another user and cannot be revealed.",
        )
    env_key = _normalize_env(body.environment)
    envs = doc.get("environments") or {}
    env_data = envs.get(env_key) or {}

    key = (body.key or "").strip()
    if not key:
        return {"error": "Field key is required."}

    persona = (body.persona or "").strip()
    if persona:
        personas = env_data.get("personas") or {}
        creds = personas.get(persona)
        if not isinstance(creds, dict) or key not in creds:
            return {
                "error": (
                    f"Field '{key}' is not present for persona '{persona}' "
                    f"in environment '{env_key}'."
                )
            }
        value = creds.get(key)
    else:
        if key not in env_data:
            return {"error": f"Field '{key}' is not present for environment '{env_key}'."}
        value = env_data.get(key)

    try:
        plain = _decrypt(value)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "Test-data reveal: app=%s env=%s key=%s persona=%s",
        body.application_id,
        env_key,
        key,
        persona or None,
    )
    return {"key": key, "value": plain, **({"persona": persona} if persona else {})}


@router.get("/test-data/scenario/{filename}")
def get_scenario_data(filename: str):
    safe = SCENARIO_DIR / Path(filename).name
    return _read(safe)


@router.put("/test-data/scenario/{filename}")
def save_scenario_data(filename: str, body: TestDataBody):
    safe = SCENARIO_DIR / Path(filename).name
    content = dict(body.content)
    content.pop("password", None)
    _write(safe, content)
    return {"saved": True}
