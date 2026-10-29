"""Permits (C4): consume only from a live, unrevoked, unexpired, current-token permit."""
from smartdialer.safety.controller import SafetyController
from smartdialer.safety.leader import try_lead
from tests.helpers import issue_permit, make_campaign

consume = SafetyController.consume_one


def test_consume_until_exhausted(conn):
    cid = make_campaign(conn)
    issue_permit(conn, cid, approved=2)
    assert consume(conn, cid) and consume(conn, cid)
    assert consume(conn, cid) is None


def test_revoked_permit_cannot_be_consumed(conn):
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=5)
    conn.execute("UPDATE dial_permits SET revoked = true WHERE id = %s", (pid,))
    assert consume(conn, cid) is None
    assert conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"] == 0


def test_expired_permit_cannot_be_consumed(conn):
    cid = make_campaign(conn)
    issue_permit(conn, cid, approved=5, ttl_ms=-1)
    assert consume(conn, cid) is None


def test_deposed_leaders_permit_dies_on_takeover(conn):
    cid = make_campaign(conn)
    token = try_lead(conn, cid, "w1", lease_ms=1)
    issue_permit(conn, cid, approved=5, token=token)
    assert consume(conn, cid)
    conn.execute("SELECT pg_sleep(0.01)")
    new_token = try_lead(conn, cid, "w2", lease_ms=3000)    # w1's lease expired: takeover
    assert new_token == token + 1
    assert consume(conn, cid) is None, "w1's permit carries the old token"


def test_lease_is_exclusive_and_renewable(conn):
    cid = make_campaign(conn)
    t1 = try_lead(conn, cid, "w1", lease_ms=3000)
    assert try_lead(conn, cid, "w2", lease_ms=3000) is None
    assert try_lead(conn, cid, "w1", lease_ms=3000) == t1, "renewal keeps the token"


def test_rollback_returns_permit_unit(conn):
    import psycopg
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=1)
    with conn.transaction():
        assert consume(conn, cid)
        raise psycopg.Rollback()
    assert conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"] == 0
