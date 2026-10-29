"""The last line of defence: constraints that hold even if application code is wrong."""
import uuid

import psycopg
import pytest


def _campaign(conn):
    cid = conn.execute("INSERT INTO campaigns (name, mode) VALUES ('t', 'PROGRESSIVE') RETURNING id").fetchone()["id"]
    bid = conn.execute("INSERT INTO borrowers (campaign_id, phone) VALUES (%s, '+910') RETURNING id", (cid,)).fetchone()["id"]
    aid = conn.execute("INSERT INTO agents (campaign_id, name) VALUES (%s, 'a') RETURNING id", (cid,)).fetchone()["id"]
    return cid, bid, aid


def _call(conn, cid, bid, aid, state="RESERVED"):
    cid_ = uuid.uuid4()
    conn.execute(
        "INSERT INTO calls (id, campaign_id, borrower_id, agent_id, mode, state, state_rank, provider_name)"
        " VALUES (%s, %s, %s, %s, 'PROGRESSIVE', %s, 1, 'A')", (cid_, cid, bid, aid, state))
    return cid_


def test_one_live_call_per_borrower(conn):
    cid, bid, _ = _campaign(conn)
    _call(conn, cid, bid, None)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _call(conn, cid, bid, None)


def test_terminal_calls_do_not_block_borrower(conn):
    cid, bid, _ = _campaign(conn)
    _call(conn, cid, bid, None, state="FAILED")
    _call(conn, cid, bid, None)   # a retry after a failed call is fine


def test_one_live_call_per_agent(conn):
    cid, bid, aid = _campaign(conn)
    bid2 = conn.execute("INSERT INTO borrowers (campaign_id, phone) VALUES (%s, '+911') RETURNING id", (cid,)).fetchone()["id"]
    _call(conn, cid, bid, aid)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _call(conn, cid, bid2, aid)


def test_transition_unique_and_on_conflict_is_silent(conn):
    cid, bid, _ = _campaign(conn)
    call = _call(conn, cid, bid, None)
    sql = ("INSERT INTO call_transitions (call_id, from_state, to_state) VALUES (%s, 'INITIATED', 'ANSWERED')"
           " ON CONFLICT (call_id, to_state) DO NOTHING RETURNING id")
    with conn.transaction():
        assert conn.execute(sql, (call,)).fetchone() is not None
        assert conn.execute(sql, (call,)).fetchone() is None   # DUPLICATE, and the tx is still usable
        conn.execute("SELECT 1")


def test_permit_cannot_be_overconsumed(conn):
    cid, _, _ = _campaign(conn)
    pid = conn.execute("INSERT INTO dial_permits (campaign_id, fencing_token, mode, approved_count, expires_at)"
                       " VALUES (%s, 1, 'PROGRESSIVE', 1, now() + interval '2 s') RETURNING id", (cid,)).fetchone()["id"]
    conn.execute("UPDATE dial_permits SET consumed_count = 1 WHERE id = %s", (pid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE dial_permits SET consumed_count = 2 WHERE id = %s", (pid,))


def test_one_active_permit_per_campaign(conn):
    cid, _, _ = _campaign(conn)
    sql = ("INSERT INTO dial_permits (campaign_id, fencing_token, mode, approved_count, expires_at)"
           " VALUES (%s, 1, 'PROGRESSIVE', 1, now() + interval '2 s')")
    conn.execute(sql, (cid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, (cid,))
