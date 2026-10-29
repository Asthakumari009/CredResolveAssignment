import multiprocessing as mp
import os

from smartdialer.allocator.allocator import CallAllocator
from smartdialer.db import connect
from smartdialer.providers.interface import DialRejected, DialUncertain
from smartdialer.safety.controller import SafetyController
from smartdialer.transitions import advance_call
from tests.helpers import FakeProvider, issue_permit, make_campaign

CTX = mp.get_context("fork")


def _allocator(provider=None, worker="w1"):
    return CallAllocator(SafetyController(worker), {"A": provider or FakeProvider()}, worker)


def _dispatcher(dsn, cid, start):
    try:
        with connect(dsn) as conn:
            alloc = _allocator(worker=f"w{os.getpid()}")
            start.wait()
            for _ in range(20):
                alloc.dispatch(conn, cid, max_calls=10, max_attempts=3)
    finally:
        os._exit(0)


def test_50_agents_never_more_than_50_agent_bound_calls(dsn, conn):
    """The assignment's progressive invariant, under 8 concurrent dispatcher processes, with a
    permit deliberately 4x too large: the agent reservation is the hard bound."""
    cid = make_campaign(conn, agents=50, borrowers=500)
    pid = issue_permit(conn, cid, approved=200)
    start = CTX.Barrier(8)
    procs = [CTX.Process(target=_dispatcher, args=(dsn, cid, start)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    r = conn.execute(
        """SELECT count(*) AS calls, count(DISTINCT agent_id) AS agents, count(*) FILTER (WHERE agent_id IS NULL) AS unbound
           FROM calls WHERE campaign_id = %s AND state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED')""",
        (cid,)).fetchone()
    assert r == {"calls": 50, "agents": 50, "unbound": 0}
    assert conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"] == 50
    assert conn.execute("SELECT count(*) AS n FROM borrowers WHERE state = 'IN_PROGRESS'").fetchone()["n"] == 50
    assert conn.execute("SELECT count(*) AS n FROM agents WHERE state = 'DIALING'").fetchone()["n"] == 50


def test_happy_path(conn):
    cid = make_campaign(conn, agents=1, borrowers=1)
    issue_permit(conn, cid, approved=1)
    [call] = _allocator().dispatch(conn, cid, 5, 3)
    for target in ("RINGING", "ANSWERED"):
        with conn.transaction():
            advance_call(conn, call["id"], target, worker_id="w")
    c = conn.execute("SELECT state, agent_id FROM calls").fetchone()
    assert c["state"] == "CONNECTED", "progressive: answered + bound agent = connected at once"
    with conn.transaction():
        advance_call(conn, call["id"], "COMPLETED", worker_id="w", duration_s=95)
    assert conn.execute("SELECT state, reserved_call_id FROM agents").fetchone()["state"] == "WRAP_UP"
    b = conn.execute("SELECT state, last_outcome FROM borrowers").fetchone()
    assert b == {"state": "DONE", "last_outcome": "CONNECTED"}
    states = [r["to_state"] for r in conn.execute("SELECT to_state FROM call_transitions ORDER BY id")]
    assert states == ["RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED", "COMPLETED"]


def test_rejected_dial_releases_agent_and_schedules_retry(conn):
    cid = make_campaign(conn, agents=1, borrowers=1)
    issue_permit(conn, cid, approved=1)
    _allocator(FakeProvider(lambda k: DialRejected("503"))).dispatch(conn, cid, 5, 3)
    assert conn.execute("SELECT state, failure_reason FROM calls").fetchone() == {"state": "FAILED", "failure_reason": "PROVIDER_ERROR"}
    assert conn.execute("SELECT state, reserved_call_id FROM agents").fetchone() == {"state": "AVAILABLE", "reserved_call_id": None}
    b = conn.execute("SELECT state, attempts, next_attempt_at > now() AS later FROM borrowers").fetchone()
    assert b == {"state": "PENDING", "attempts": 1, "later": True}


def test_no_answer_until_exhausted(conn):
    cid = make_campaign(conn, agents=1, borrowers=1, settings={"max_attempts": 2, "retry_base_s": 0.001})
    for attempt in (1, 2):
        conn.execute("UPDATE borrowers SET next_attempt_at = now()")
        issue_permit(conn, cid, approved=1)
        [call] = _allocator().dispatch(conn, cid, 1, 2)
        with conn.transaction():
            advance_call(conn, call["id"], "FAILED", worker_id="w", reason="NO_ANSWER")
    assert conn.execute("SELECT state, attempts FROM borrowers").fetchone() == {"state": "EXHAUSTED", "attempts": 2}


def test_uncertain_dial_records_nothing(conn):
    cid = make_campaign(conn, agents=1, borrowers=1)
    issue_permit(conn, cid, approved=1)
    _allocator(FakeProvider(lambda k: DialUncertain("timeout"))).dispatch(conn, cid, 5, 3)
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "RESERVED"
    assert conn.execute("SELECT state FROM agents").fetchone()["state"] == "RESERVED"


def test_no_agent_means_no_call_and_unit_returned(conn):
    cid = make_campaign(conn, agents=0, borrowers=3)
    pid = issue_permit(conn, cid, approved=3)
    assert _allocator().dispatch(conn, cid, 5, 3) == []
    assert conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"] == 0
    assert conn.execute("SELECT count(*) AS n FROM borrowers WHERE state = 'PENDING'").fetchone()["n"] == 3


def test_no_permit_no_call(conn):
    cid = make_campaign(conn, agents=5, borrowers=5)
    assert _allocator().dispatch(conn, cid, 5, 3) == []


def test_allocator_requires_safety_controller():
    import pytest
    with pytest.raises(TypeError):
        CallAllocator(object(), {}, "w")


def test_predictive_answer_claims_agent_or_waits(conn):
    cid = make_campaign(conn, mode="PREDICTIVE", agents=1, borrowers=2)
    issue_permit(conn, cid, approved=2, mode="PREDICTIVE")
    c1, c2 = _allocator().dispatch(conn, cid, 5, 3)
    assert conn.execute("SELECT count(*) AS n FROM calls WHERE agent_id IS NULL").fetchone()["n"] == 2
    with conn.transaction():
        _, row1 = advance_call(conn, c1["id"], "ANSWERED", worker_id="w")
    with conn.transaction():
        _, row2 = advance_call(conn, c2["id"], "ANSWERED", worker_id="w")
    assert row1["state"] == "CONNECTED" and row1["agent_id"] is not None
    assert row2["state"] == "ANSWERED" and row2["agent_id"] is None, "no agent left: waits for answer-hold"
