"""Applies migrations/*.sql in name order, each once, each in its own transaction."""
import sys
from pathlib import Path

from smartdialer.db import DATABASE_URL, connect

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def migrate(dsn: str | None = None) -> list[str]:
    applied = []
    with connect(dsn) as conn:
        # Two processes migrating at once would race on CREATE TABLE; serialize them.
        conn.execute("SELECT pg_advisory_lock(4242)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.execute("SELECT pg_advisory_unlock(4242)")
    return applied


if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else DATABASE_URL
    print("applied:", migrate(dsn) or "nothing, up to date")
