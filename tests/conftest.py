import os

import psycopg
import pytest

from smartdialer.migrate import migrate

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://dialer:dialer@localhost:55432/dialer_test")
TABLES = ("provider_events", "call_transitions", "agent_transitions", "safety_decisions", "calls",
          "dial_permits", "borrowers", "agents", "workers", "campaigns")


@pytest.fixture(scope="session")
def dsn():
    """A fresh test database, migrated once per session."""
    base, name = TEST_DSN.rsplit("/", 1)
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    migrate(TEST_DSN)
    os.environ["DATABASE_URL"] = TEST_DSN   # for child processes spawned by tests
    return TEST_DSN


@pytest.fixture
def conn(dsn):
    from smartdialer.db import connect
    with connect(dsn) as c:
        c.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        c.execute("UPDATE provider_health SET breaker_state='CLOSED', opened_at=NULL")
        yield c
