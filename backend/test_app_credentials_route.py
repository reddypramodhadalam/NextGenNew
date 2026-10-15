"""
test_app_credentials_route.py

Isolation test for the per-user application credential route
(api/routes/app_credentials.py).

Proves the thing the whole feature rests on: calling the SAME endpoint for the
SAME application as two different users returns each user only THEIR OWN
credential — never the other user's. The owning user is resolved server-side
from the identity header; the request body never carries a user id.

Run from the backend/ directory:
    $env:PYTHONIOENCODING="utf-8"
    ../.venv/Scripts/python.exe test_app_credentials_route.py

Seeds throwaway users + one throwaway application, then deletes them (the
app_credentials rows cascade with the application).
"""

import sys
import time

from core.db.connection import get_conn
from core.db.queries import applications as apps_db
from api.routes import app_credentials as route
from api.routes.app_credentials import CredentialBody, MASKED_SECRET


SUFFIX = str(int(time.time()))
USER_A = f"alice+{SUFFIX}@example.test"
USER_B = f"bob+{SUFFIX}@example.test"


class FakeRequest:
    """Minimal stand-in for a Starlette Request: the route only reads
    request.headers.get(...), so a case-insensitive header map is enough."""

    def __init__(self, email):
        self._headers = {"x-triggered-by": email}

    @property
    def headers(self):
        return self

    def get(self, name, default=None):
        return self._headers.get(name.lower(), default)


_passed = 0
_failed = 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}")


def _cleanup(app_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if app_id is not None:
                cur.execute("DELETE FROM applications WHERE id = %s", (app_id,))
            cur.execute(
                "DELETE FROM users WHERE email IN (%s, %s)", (USER_A, USER_B)
            )


def main():
    from core.db.connection import init_pool
    init_pool()

    app = apps_db.create(
        name=f"CredRouteTest {SUFFIX}",
        base_url=None,
        normalized_url=None,
        application_type=None,
        owner=None,
    )
    app_id = app["id"]
    req_a = FakeRequest(USER_A)
    req_b = FakeRequest(USER_B)

    try:
        # Alice saves her login.
        saved = route.save_app_credential(
            app_id, CredentialBody(username="alice_login", password="alice_secret"), req_a
        )
        check("Alice save returns her username", saved["username"] == "alice_login")
        check("Alice save masks the password", saved["password"] == MASKED_SECRET)
        check("Alice save reports a stored password", saved["has_password"] is True)

        # Bob, hitting the SAME application, sees nothing of Alice's.
        bob_view = route.get_app_credential(app_id, req_b)
        check("Bob sees empty username (not Alice's)", bob_view["username"] == "")
        check("Bob sees no stored password", bob_view["has_password"] is False)

        # Alice's own GET is masked, never plaintext.
        alice_view = route.get_app_credential(app_id, req_a)
        check("Alice GET is masked", alice_view["password"] == MASKED_SECRET)
        check("Alice GET keeps her username", alice_view["username"] == "alice_login")

        # Reveal is scoped: Alice reveals her own; Bob gets a 404.
        revealed = route.reveal_app_credential(app_id, req_a)
        check("Alice reveal returns her plaintext", revealed["password"] == "alice_secret")

        bob_reveal_blocked = False
        try:
            route.reveal_app_credential(app_id, req_b)
        except Exception as e:  # HTTPException(404)
            bob_reveal_blocked = getattr(e, "status_code", None) == 404
        check("Bob cannot reveal (404, nothing of his)", bob_reveal_blocked)

        # Bob saves his OWN login; the two coexist independently.
        route.save_app_credential(
            app_id, CredentialBody(username="bob_login", password="bob_secret"), req_b
        )
        a_again = route.reveal_app_credential(app_id, req_a)
        b_again = route.reveal_app_credential(app_id, req_b)
        check("Alice still sees her own secret", a_again["password"] == "alice_secret")
        check("Bob sees his own, different secret", b_again["password"] == "bob_secret")

        # Username-only update keeps the stored password (masked placeholder).
        route.save_app_credential(
            app_id, CredentialBody(username="alice_login_v2", password=MASKED_SECRET), req_a
        )
        a_kept = route.reveal_app_credential(app_id, req_a)
        check("Username-only edit keeps Alice's password", a_kept["password"] == "alice_secret")
        check("Username-only edit updates the username", a_kept["username"] == "alice_login_v2")

        # Delete is scoped: Alice's delete does not touch Bob's.
        route.delete_app_credential(app_id, req_a)
        check("Alice delete removes hers", route.get_app_credential(app_id, req_a)["has_password"] is False)
        check("Bob's credential survives Alice's delete", route.get_app_credential(app_id, req_b)["has_password"] is True)
    finally:
        _cleanup(app_id)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
