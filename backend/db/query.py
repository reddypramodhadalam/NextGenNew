"""
db/query.py

Runs one ad-hoc SQL statement against the configured database and prints the
result as a simple table.

A development and verification convenience, not part of the application. It
exists so a schema check does not require psql on PATH or a fragile shell
one-liner, and so the connection details still come from one place
(core.settings) rather than being typed each time.

Usage (from the backend directory). Wrap the SQL in DOUBLE quotes so any
single quotes inside it survive PowerShell:

    python db/query.py "SELECT id, slug, status FROM help_articles ORDER BY id"
    python db/query.py "SELECT count(*) FROM help_chunks"

Reads only what you give it — it will happily run a write statement, so treat
it as you would a psql prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

# Make the backend package importable when this script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from core.settings import settings  # noqa: E402


def run(sql: str) -> None:
    """Execute one SQL statement and print any rows it returns.

    Args:
        sql: The SQL statement to execute.

    Raises:
        psycopg2.Error: If the statement fails. The error is printed plainly
            first so the cause is readable without a stack trace.
    """
    parsed = urlparse(settings.db.url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)

                if cur.description is None:
                    print(f"OK ({cur.rowcount} row(s) affected)")
                    return

                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                _print_table(cols, rows)
                print(f"\n({len(rows)} row(s))")
    except psycopg2.Error as e:
        print("FAILED")
        print(f"  {e.pgerror or e}".rstrip())
        raise
    finally:
        conn.close()


def _print_table(cols: list[str], rows: list[tuple]) -> None:
    """Print columns and rows aligned, truncating very wide values.

    Args:
        cols: Column names from the cursor description.
        rows: The fetched rows.
    """
    def cell(v) -> str:
        s = "NULL" if v is None else str(v)
        return s if len(s) <= 60 else s[:57] + "..."

    table = [cols] + [[cell(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(cols))]

    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in table[1:]:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    """Command-line entry point."""
    if len(sys.argv) < 2:
        print('Usage: python db/query.py "SELECT ..."')
        sys.exit(2)

    try:
        run(" ".join(sys.argv[1:]))
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
