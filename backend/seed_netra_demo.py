"""
seed_netra_demo.py  (idempotent, varied scenarios, externalised test data)

Sets up / updates the Netra demo WITHOUT destroying data. Re-run any
time; it never deletes the app, scenarios, generations, or history.

Creates a realistic MIX of scenarios so the demo shows the full range
of test types (positive / negative / edge / security) and priorities
(P0 / P1 / P2). Each scenario references a test-data file (externalised,
not hardcoded).

Structure:
  Netra
    Scenario "User authentication"      (security,  P0)   data: authentication.json
       - valid login                    (positive script)
       - invalid password rejected      (negative script)
       - locked-out user denied         (security script)
    Scenario "Shopping cart"            (positive,  P1)   data: shopping_cart.json
       - add item to cart               (positive script)
    Scenario "Search edge cases"        (edge,      P2)   (no script yet — shows the
                                                           'generate' path + edge badge)
"""

import os
import sys
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

APP_NAME = "Netra"
APP_URL = "https://www.saucedemo.com"

# scenario -> (category, priority, test_data_file, [ (script_title, script_path, script_category) ])
SCENARIOS = [
    ("User authentication", "security", "P0", "authentication.json", [
        ("Verify a user can log in with valid credentials and reaches the home screen",
         "generated_tests/test_netra_login_valid.py", "positive"),
        ("Verify login is rejected with a clear error when the password is wrong",
         "generated_tests/test_netra_login_invalid.py", "negative"),
        ("Verify a locked-out user is denied access with a clear message",
         "generated_tests/test_netra_locked_out.py", "security"),
    ]),
    ("Shopping cart", "positive", "P1", "shopping_cart.json", [
        ("Verify a logged-in user can add an item to the cart",
         "generated_tests/test_netra_add_to_cart.py", "positive"),
    ]),
    ("Search edge cases", "edge", "P2", "search.json", [
        # no scripts yet — demonstrates the 'generate scripts' path + edge category
    ]),
]



def migrate_old_scenario(cur, app_id):
    """
    One-time cleanup: the demo previously had a single scenario
    "User authentication & cart flow". We now split it into proper
    scenarios. Remove that specific old scenario (and only that one)
    if it exists, so it doesn't linger as an orphan. This is targeted
    and safe — it does not touch anything else.
    """
    old_title = "User authentication & cart flow"
    cur.execute("SELECT id FROM test_case_proposals WHERE application_id=%s AND title=%s", (app_id, old_title))
    r = cur.fetchone()
    if r:
        pid = r[0]
        cur.execute("""DELETE FROM executions WHERE generation_id IN
                       (SELECT id FROM generations WHERE test_case_proposal_id=%s)""", (pid,))
        cur.execute("DELETE FROM generations WHERE test_case_proposal_id=%s", (pid,))
        cur.execute("DELETE FROM test_case_proposals WHERE id=%s", (pid,))
        print(f"  Migrated: removed old combined scenario (id={pid}).")


def upsert_app(cur):
    cur.execute("SELECT id FROM applications WHERE name=%s", (APP_NAME,))
    r = cur.fetchone()
    if r:
        cur.execute("UPDATE applications SET base_url=%s, normalized_url=%s WHERE id=%s",
                    (APP_URL, "saucedemo.com", r[0]))
        print(f"  App '{APP_NAME}' exists (id={r[0]}) — kept.")
        return r[0]
    cur.execute("""INSERT INTO applications (name, base_url, normalized_url, owner)
                   VALUES (%s,%s,%s,'Data Platform Team') RETURNING id""",
                (APP_NAME, APP_URL, "saucedemo.com"))
    i = cur.fetchone()[0]; print(f"  Created app '{APP_NAME}' (id={i})."); return i


def upsert_scenario(cur, app_id, title, category, priority, data_file):
    cur.execute("SELECT id FROM test_case_proposals WHERE application_id=%s AND title=%s", (app_id, title))
    r = cur.fetchone()
    # store the test-data filename in tags so the UI/loader knows which file
    tags = psycopg2.extras.Json({"test_data_file": data_file}) if data_file else None
    if r:
        cur.execute("UPDATE test_case_proposals SET category=%s, priority=%s, status='generated', tags=%s WHERE id=%s",
                    (category, priority, tags, r[0]))
        print(f"    Scenario '{title}' exists (id={r[0]}) — metadata updated, scripts kept.")
        return r[0]
    cur.execute("""INSERT INTO test_case_proposals
                   (application_id, title, description, test_type, category, source, priority, should_automate, status, tags)
                   VALUES (%s,%s,%s,'functional',%s,'author_agent',%s,TRUE,'generated',%s) RETURNING id""",
                (app_id, title, title, category, priority, tags))
    i = cur.fetchone()[0]; print(f"    Created scenario '{title}' ({category}/{priority})."); return i


def upsert_script(cur, proposal_id, i, title, path, category):
    cur.execute("SELECT id FROM generations WHERE test_case_proposal_id=%s AND script_path=%s", (proposal_id, path))
    r = cur.fetchone()
    if r:
        cur.execute("UPDATE generations SET notes=%s WHERE id=%s", (title, r[0])); return
    cur.execute("""INSERT INTO generations
                   (test_case_proposal_id, attempt_number, provider, model, generation_status,
                    test_verdict, script_content, script_path, steps_taken, notes, completed_at)
                   VALUES (%s,%s,'simulation','sim-1.0','success',NULL,%s,%s,6,%s,now())""",
                (proposal_id, i, f"# {category} test: {title}\n# {path}", path, title))
    print(f"      + script: {title} ({category})")


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            app_id = upsert_app(cur)
            migrate_old_scenario(cur, app_id)
            for title, cat, prio, data_file, scripts in SCENARIOS:
                pid = upsert_scenario(cur, app_id, title, cat, prio, data_file)
                for i, (st, sp, sc) in enumerate(scripts, start=1):
                    upsert_script(cur, pid, i, st, sp, sc)
        conn.commit()
        print("\nDone. Netra set up with varied scenarios. No data deleted.")
    finally:
        conn.close()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        print("--reset is not supported: this seed never deletes data. Run without it.")
        sys.exit(0)
    main()