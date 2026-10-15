"""
core/db/queries/app_credentials.py

All database reads and writes for the `app_credentials` table — one login
credential per (application, user). No other module touches this table
directly.

THE SINGLE DOOR
    get_credential(application_id, user_id) is the ONLY way any stage
    (crawl, generation, execution) obtains a stored password. It always
    filters by BOTH application_id AND user_id, so a run can only ever read
    the credential of the user who initiated it — never another user's. There
    is deliberately no function that returns "all credentials for an
    application", and no function that accepts a password back out in bulk.

SECRECY
    The password is encrypted at rest with Fernet, using the SAME key as the
    existing test-data store (TEST_DATA_ENCRYPTION_KEY). The plaintext is only
    ever held in memory, at save time and at the moment of use. The username
    is stored in clear (it is not a secret and callers routinely need to
    display it); only the password is encrypted.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from core.db.connection import get_conn

logger = logging.getLogger(__name__)

SECRET_ALG = "fernet"


def _fernet() -> Fernet:
    """Build a Fernet from TEST_DATA_ENCRYPTION_KEY — the same key the
    test-data store uses, so one key decrypts every stored secret.

    Raises RuntimeError with an actionable message if the key is missing or
    malformed, rather than failing obscurely deep inside a save.
    """
    key = (os.environ.get("TEST_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        # Some processes start from the repo root rather than backend/, so
        # implicit dotenv discovery can miss backend/.env. Load it directly.
        backend_env = Path(__file__).resolve().parents[3] / ".env"
        if backend_env.exists():
            load_dotenv(dotenv_path=backend_env, override=False)
        key = (os.environ.get("TEST_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "TEST_DATA_ENCRYPTION_KEY is not set. Configure it before saving "
            "or reading per-user application credentials."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            "TEST_DATA_ENCRYPTION_KEY is invalid. It must be a valid Fernet key."
        ) from e


def upsert_credential(application_id: int, user_id: int, username: str, password: str) -> dict:
    """Save (or replace) THIS user's credential for THIS application.

    The password is encrypted before it ever reaches the database. A second
    call for the same (application, user) updates the existing row — enforced
    by the UNIQUE(application_id, user_id) constraint via ON CONFLICT.

    Args:
        application_id: The application this credential logs into.
        user_id: The internal users.id of the credential's owner.
        username: The login username (stored in clear — not a secret).
        password: The login password (encrypted at rest, never stored plain).

    Returns:
        A dict of the row's non-secret metadata (id, application_id, user_id,
        username, created_at, updated_at). Never returns the password.
    """
    cipher = _fernet().encrypt(password.encode("utf-8")).decode("utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_credentials
                    (application_id, user_id, username, secret_alg, secret_cipher)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (application_id, user_id) DO UPDATE
                    SET username      = EXCLUDED.username,
                        secret_alg    = EXCLUDED.secret_alg,
                        secret_cipher = EXCLUDED.secret_cipher,
                        updated_at    = now()
                RETURNING id, application_id, user_id, username, created_at, updated_at
                """,
                (application_id, user_id, username, SECRET_ALG, cipher),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
    logger.info(
        "app_credentials: saved credential app=%s user=%s", application_id, user_id
    )
    return dict(zip(cols, row))


def get_credential(application_id: int, user_id: int) -> Optional[dict]:
    """THE SINGLE DOOR. Return THIS user's decrypted credential for THIS
    application, or None if they have not saved one.

    Always scoped by BOTH application_id and user_id. There is no fallback to
    any other user's row: if this user has no credential for this application,
    the result is None — never someone else's credential.

    Args:
        application_id: The application being logged into.
        user_id: The internal users.id of the run's initiator.

    Returns:
        {"username": str, "password": str} with the password decrypted in
        memory, or None if no row exists for this exact (application, user).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT username, secret_cipher
                FROM app_credentials
                WHERE application_id = %s AND user_id = %s
                """,
                (application_id, user_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    username, cipher = row[0], row[1]
    try:
        password = _fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        # A wrong/rotated key, or a tampered row, must fail loudly — never
        # silently return a bad or empty password that a stage would type.
        raise RuntimeError(
            "Could not decrypt stored credential with current "
            "TEST_DATA_ENCRYPTION_KEY (app=%s user=%s)." % (application_id, user_id)
        ) from e
    return {"username": username, "password": password}


def has_credential(application_id: int, user_id: int) -> bool:
    """True if THIS user has a saved credential for THIS application.

    Lets a caller check presence (e.g. to show "you haven't added your login
    for this app yet") without decrypting the secret.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM app_credentials
                WHERE application_id = %s AND user_id = %s
                """,
                (application_id, user_id),
            )
            return cur.fetchone() is not None


def delete_credential(application_id: int, user_id: int) -> bool:
    """Delete THIS user's credential for THIS application.

    Returns True if a row was removed, False if there was nothing to remove.
    Scoped by both keys, so a user can only ever delete their own credential.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app_credentials
                WHERE application_id = %s AND user_id = %s
                """,
                (application_id, user_id),
            )
            removed = cur.rowcount > 0
    if removed:
        logger.info(
            "app_credentials: deleted credential app=%s user=%s",
            application_id, user_id,
        )
    return removed
