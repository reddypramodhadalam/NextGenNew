"""
test_app_credentials_pipeline.py

Stage 2b wiring test: proves the credential-env builders in
service/auth_gateway.py actually sign a run in as WHOEVER triggered it.

    - When a real user (resolvable email) who saved a per-user credential
      triggers the run, TEST_USERNAME / TEST_PASSWORD carry THEIR login.
    - When the trigger is not a real user (e.g. "anonymous"), the per-user
      credential is NOT used — behaviour falls back to the shared file-based
      test data exactly as before.
    - Two different users get two different logins for the SAME application.

No browser, LLM, or network needed. Seeds throwaway users + one throwaway
application + DB credentials, then deletes them.

Run from the backend/ directory:
    $env:PYTHONIOENCODING="utf-8"
    ../.venv/Scripts/python.exe test_app_credentials_pipeline.py
"""

import sys
import time


SUFFIX = str(int(time.time()))
USER_A = f"alice+{SUFFIX}@example.test"
USER_B = f"bob+{SUFFIX}@example.test"

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


def main():
    from core.db.connection import init_pool, get_conn
    init_pool()

    from core.db.queries import applications as apps_db
    from core.db.queries import users as users_db
    from core.db.queries import app_credentials as creds_db
    from service import auth_gateway as gw

    app = apps_db.create(
        name=f"CredPipelineTest {SUFFIX}",
        base_url="https://example.test",
        normalized_url=None,
        application_type=None,
        owner=None,
    )
    app_id = app["id"]

    user_a = users_db.get_or_create_by_email(USER_A)
    user_b = users_db.get_or_create_by_email(USER_B)
    creds_db.upsert_credential(app_id, user_a["id"], "alice_login", "alice_secret")
    creds_db.upsert_credential(app_id, user_b["id"], "bob_login", "bob_secret")

    try:
        # Alice triggers -> her login drives the env.
        env_a = gw.build_precheck_test_env(app, triggered_by=USER_A)
        check("Alice precheck TEST_USERNAME is hers", env_a.get("TEST_USERNAME") == "alice_login")
        check("Alice precheck TEST_PASSWORD is hers", env_a.get("TEST_PASSWORD") == "alice_secret")

        # Bob triggers the SAME app -> his own, different login.
        env_b = gw.build_precheck_test_env(app, triggered_by=USER_B)
        check("Bob precheck TEST_USERNAME is his", env_b.get("TEST_USERNAME") == "bob_login")
        check("Bob precheck TEST_PASSWORD is his", env_b.get("TEST_PASSWORD") == "bob_secret")

        # Scenario env overlay works the same way.
        sc_a = gw.build_scenario_test_env(app, None, None, triggered_by=USER_A)
        check("Alice scenario TEST_USERNAME is hers", sc_a.get("TEST_USERNAME") == "alice_login")
        check("Alice scenario TEST_PASSWORD is hers", sc_a.get("TEST_PASSWORD") == "alice_secret")

        # _load_credentials prefers the per-user DB credential.
        loaded = gw._load_credentials(app, triggered_by=USER_A)
        check("_load_credentials returns Alice's DB login",
              loaded == {"username": "alice_login", "password": "alice_secret"})

        # Non-user trigger ("anonymous") must NOT pull anyone's per-user login.
        env_anon = gw.build_precheck_test_env(app, triggered_by="anonymous")
        check("anonymous trigger does NOT get Alice's password",
              env_anon.get("TEST_PASSWORD") != "alice_secret")
        check("anonymous trigger does NOT get Bob's password",
              env_anon.get("TEST_PASSWORD") != "bob_secret")

        # Default (no triggered_by) preserves the original file-based behaviour:
        # no per-user secret leaks in.
        env_default = gw.build_precheck_test_env(app)
        check("default (no trigger) does NOT get a per-user password",
              env_default.get("TEST_PASSWORD") not in ("alice_secret", "bob_secret"))
    finally:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM applications WHERE id = %s", (app_id,))
                cur.execute("DELETE FROM users WHERE email IN (%s, %s)", (USER_A, USER_B))

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
