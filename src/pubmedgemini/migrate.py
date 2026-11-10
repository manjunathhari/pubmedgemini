import re
from pathlib import Path

from psycopg_pool import ConnectionPool

LOCK_ID = 7310
FILE_PATTERN = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")


def apply_migrations(pool: ConnectionPool, migrations_dir: Path) -> list[str]:
    """Apply numbered SQL files once each, in name order. Returns the names applied now."""
    files = sorted(
        p for p in Path(migrations_dir).iterdir() if p.is_file() and FILE_PATTERN.match(p.name)
    )
    applied: list[str] = []
    with pool.connection() as conn:
        conn.autocommit = True
        try:
            conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
            try:
                with conn.transaction():
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations ("
                        "name TEXT PRIMARY KEY, "
                        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                    )
                done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
                for path in files:
                    if path.name in done:
                        continue
                    with conn.transaction():
                        conn.execute(path.read_text())
                        conn.execute(
                            "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                        )
                    applied.append(path.name)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        finally:
            conn.autocommit = False
    return applied
