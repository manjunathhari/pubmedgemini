from psycopg_pool import ConnectionPool


def open_pool(database_url: str) -> ConnectionPool:
    """Open a pool without waiting, so an unreachable database does not block start-up."""
    pool = ConnectionPool(
        database_url, min_size=1, max_size=5, timeout=5, open=False
    )
    pool.open(wait=False)
    return pool


def is_healthy(pool: ConnectionPool) -> bool:
    """Run SELECT 1. Returns False on any database error; never raises."""
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - health check must never raise
        return False
