"""Database access. Plain psycopg 3, no ORM, so every lock is visible in the SQL.

Connections are autocommit; every unit of work is an explicit `with conn.transaction():`
block, so transaction boundaries are visible in the code. Isolation is READ COMMITTED with
explicit row locks: every race in this system is on a specific row and is closed by a
conditional UPDATE, SELECT ... FOR UPDATE [SKIP LOCKED], or a unique index.

LOCK ORDER (used by every transaction that locks more than one of these rows):

    campaign / permit / inbox event  ->  call  ->  borrower  ->  agent

(dispatch pins the permit FOR KEY SHARE -> borrower -> agent and then inserts a brand-new call
row; claiming permit units is a separate one-statement transaction; the Safety Controller takes
campaign -> permit; the event processor takes inbox event -> call -> borrower -> agent.)

Two transactions taking the same rows in opposite orders could deadlock. Postgres would
detect that and abort one of them, so it would not corrupt data, but under load it turns
into retries. With a single global order it cannot happen. Each multi-row transaction is
marked with a `# LOCK ORDER:` comment.
"""
import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://dialer:dialer@localhost:55432/dialer")


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or DATABASE_URL, autocommit=True, row_factory=dict_row)
