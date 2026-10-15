"""Read-only, application-scoped captured maps for generation coverage."""

from core.db.connection import get_conn


def list_complete(application_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id, crawled_at, map_data
                       FROM application_maps
                       WHERE application_id=%s AND status='complete'
                       ORDER BY crawled_at DESC NULLS LAST, id DESC''', (application_id,))
        columns = [column[0] for column in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]