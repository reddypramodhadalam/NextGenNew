"""
seed_database.py

Populates the database with demo data for the NextGen2 dashboard.

WHY THIS EXISTS (read before editing):
This is NOT throwaway code. The demo data lives in seed_data.json — a
plain, editable file. This script reads that JSON and inserts it into
Postgres using the real schema. To change what apps/scenarios appear,
edit seed_data.json ONLY. Never hardcode data in components or here.

Baxter developers can:
  - edit seed_data.json to add real apps/scenarios
  - re-run this script any time to reset demo data
  - delete it entirely for production — nothing depends on it

It inserts a full, realistic chain per app:
  application -> proposals (scenarios) -> generations -> a batch ->
  executions (pass/fail history), so coverage %, pass rates, the trend
  chart and the donut all populate.

Usage (from the backend folder, with .env configured):
    python seed_database.py
    python seed_database.py --reset      # wipe seeded data first
"""

import json
import os
import sys
import random
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATA_FILE = Path(__file__).parent / "seed_data.json"


def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set (check your .env).")
        sys.exit(1)
    return psycopg2.connect(url)


def reset(cur):
    """Remove previously seeded rows (keeps schema intact)."""
    # Order matters for FKs: executions -> batches/generations -> proposals -> apps
    cur.execute("DELETE FROM executions")
    cur.execute("DELETE FROM batches")
    cur.execute("DELETE FROM generations")
    cur.execute("DELETE FROM test_case_proposals")
    cur.execute("DELETE FROM applications")
    print("  Cleared existing data.")


def normalize(url: str) -> str:
    return (url or "").replace("https://", "").replace("http://", "").rstrip("/")


def seed(cur, data: dict):
    apps = data["applications"]
    total_scen = total_exec = 0

    for app in apps:
        # 1. application
        cur.execute(
            """INSERT INTO applications (name, base_url, normalized_url, owner)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (app["name"], app["base_url"], normalize(app["base_url"]), app["owner"]),
        )
        app_id = cur.fetchone()[0]

        # 2. one batch per app (a run grouping)
        cur.execute(
            """INSERT INTO batches (application_id, environment, trigger_type, status, completed_at)
               VALUES (%s, %s, 'manual', 'completed', now() - (random() * interval '3 days'))
               RETURNING id""",
            (app_id, app.get("environment", "default")),
        )
        batch_id = cur.fetchone()[0]

        for scen in app["scenarios"]:
            # 3. proposal (scenario)
            cur.execute(
                """INSERT INTO test_case_proposals
                   (application_id, title, description, test_type, category, source, priority, should_automate, status)
                   VALUES (%s, %s, %s, 'functional', %s, 'author_agent', %s, TRUE, %s)
                   RETURNING id""",
                (app_id, scen["title"], scen["title"], scen.get("category"),
                 scen.get("priority"), scen["status"]),
            )
            proposal_id = cur.fetchone()[0]
            total_scen += 1

            # 4. generation + execution only for 'generated' scenarios
            if scen["status"] == "generated":
                verdict = scen.get("verdict") or "pass"
                cur.execute(
                    """INSERT INTO generations
                       (test_case_proposal_id, provider, model, generation_status, test_verdict,
                        script_content, script_path, steps_taken, completed_at)
                       VALUES (%s, 'simulation', 'sim-1.0', 'success', %s, %s, %s, %s,
                               now() - (random() * interval '3 days'))
                       RETURNING id""",
                    (proposal_id, verdict,
                     f"# Simulated pytest+selenium script for: {scen['title']}",
                     f"generated_tests/test_{proposal_id}.py",
                     random.randint(5, 14)),
                )
                gen_id = cur.fetchone()[0]

                cur.execute(
                    """INSERT INTO executions
                       (generation_id, batch_id, result, output, duration_ms, executed_at)
                       VALUES (%s, %s, %s, %s, %s, now() - (random() * interval '3 days'))""",
                    (gen_id, batch_id, verdict,
                     f"Simulated run output for {scen['title']}",
                     random.randint(1200, 7000)),
                )
                total_exec += 1

        print(f"  {app['name']}: {len(app['scenarios'])} scenarios inserted.")

    return len(apps), total_scen, total_exec


def main():
    do_reset = "--reset" in sys.argv
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if do_reset:
                reset(cur)
            n_apps, n_scen, n_exec = seed(cur, data)
        conn.commit()
        print(f"\nDone. Seeded {n_apps} applications, {n_scen} scenarios, {n_exec} executions.")
        print("Refresh the dashboard to see them.")
    except psycopg2.Error as e:
        conn.rollback()
        print(f"\nDatabase error: {e}")
        print("If it's a duplicate error, run:  python seed_database.py --reset")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
