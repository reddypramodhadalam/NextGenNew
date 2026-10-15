"""
core/db/connection.py

Opens and manages the database connection pool.
Every query module imports `get_conn()` from here — nothing else
constructs its own connection.

Uses psycopg2 with a simple connection pool. For production scale,
swap the pool implementation here without touching any query module.
"""

import logging
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from core.settings import settings

logger = logging.getLogger(__name__)

_pool: pool.ThreadedConnectionPool | None = None


def init_pool() -> None:
    """
    Initialises the connection pool.
    Called once at startup from api/main.py.

    Uses explicit keyword arguments instead of a DSN string because
    psycopg2 on Windows ignores the host in a DSN and attempts a
    Unix socket connection, which Windows does not support.
    """
    global _pool
    logger.info("Initialising database connection pool")

    from urllib.parse import urlparse
    parsed = urlparse(settings.db.url)
    _pool = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )
    logger.info("Database connection pool ready")


@contextmanager
def get_conn():
    """
    Context manager that yields a database connection from the pool
    and returns it when the block exits, whether or not an exception
    was raised.

    Usage:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)

    Raises: RuntimeError if init_pool() has not been called yet.
    """
    if _pool is None:
        raise RuntimeError(
            "Database pool is not initialised. "
            "Call init_pool() during application startup."
        )
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
