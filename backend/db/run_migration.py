"""
db/run_migration.py

Applies a .sql migration file to the configured database.

Exists because psql is not always on PATH on Windows, and because the
connection details should come from one place. The database URL is read from
core.settings (the single config entry point) — nothing is typed on the
command line and nothing is hardcoded here.

The whole file is sent as one statement to the server, so dollar-quoted
DO $$ ... $$ blocks and multi-statement migrations run exactly as psql would
run them. The migration is wrapped in a single transaction: if any statement
fails, nothing is applied and the database is left as it was.

Usage (from the backend directory):
    python db/run_migration.py db/migrations/012_help_and_support.sql
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

# Make the backend package importable when this script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from core.settings import settings  # noqa: E402


def apply_migration(sql_path: Path) -> None:
    """Apply one .sql file to the configured database in a single transaction.

    Args:
        sql_path: Path to the .sql migration file to run.

    Raises:
        FileNotFoundError: If the migration file does not exist.
        psycopg2.Error: If any statement in the migration fails. The
            transaction is rolled back before the error propagates, so a
            failed migration never leaves the schema half-applied.
    """
    if not sql_path.exists():
        raise FileNotFoundError(f"Migration file not found: {sql_path}")

    sql = sql_path.read_text(encoding="utf-8")

    # Explicit keyword arguments rather than the DSN string, for the same
    # reason core/db/connection.py does it: psycopg2 on Windows ignores the
    # host in a DSN and attempts a Unix socket connection.
    parsed = urlparse(settings.db.url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )

    print(f"Database : {parsed.path.lstrip('/')} on {parsed.hostname}:{parsed.port or 5432}")
    print(f"Migration: {sql_path.name}")

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        print("Applied successfully.")
    except psycopg2.Error as e:
        # psycopg2 puts the useful detail on the diagnostics object; print it
        # plainly so the failure is actionable rather than a stack trace.
        print("FAILED — nothing was applied.")
        print(f"  {e.pgerror or e}".rstrip())
        if getattr(e, "diag", None) and e.diag.message_detail:
            print(f"  detail: {e.diag.message_detail}")
        raise
    finally:
        conn.close()


def main() -> None:
    """Command-line entry point."""
    if len(sys.argv) != 2:
        print("Usage: python db/run_migration.py <path-to-.sql>")
        sys.exit(2)

    try:
        apply_migration(Path(sys.argv[1]))
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
