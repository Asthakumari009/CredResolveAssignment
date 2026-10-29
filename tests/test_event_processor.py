import json
import random
import uuid

import pytest

from smartdialer.allocator.allocator import CallAllocator
from smartdialer.events.processor import process_batch, process_one
from smartdialer.invariants import check_all
from smartdialer.safety.controller import SafetyController
from tests.helpers import FakeProvider, issue_permit, make_campaign


def _one_call(conn, mode="PROGRESSIVE", agents=1):
    cid = make_campaign(conn, mode=mode, agents=agents, borrowers=1)
    issue_permit(conn, cid, approved=1, mode=mode)
    [call] = CallAllocator(SafetyController("w"), {"A": FakeProvider()}, "w").dispatch(conn, cid, 1, 3)
    return call["id"]


def post(conn, call_id, etype, event_id=None, duration_s=None):
    """What the webhook does: insert into the inbox, dropping exact redeliveries."""
    row = conn.execute(
        """INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, payload)
           VALUES ('A', %s, %s, %s, %s) ON CONFLICT (provider_name, provider_event_id) DO NOTHING RETURNING id""",
        (event_id or uuid.uuid4().hex, call_id, etype, json.dumps({"duration_s": duration_s}))).fetchone()
    return row is not None


def results(conn):
    return [r["processing_result"] for r in conn.execute("SELECT processing_result FROM provider_events ORDER BY id")]


def transitions(conn, call_id):
    return [r["to_state"] for r in conn.execute("SELECT to_state FROM call_transitions WHERE call_id = %s ORDER BY id", (call_id,))]


def test_answered_five_times_one_transition(conn):
    call = _one_call(conn)
    stored = [post(conn, call, "ANSWERED", event_id="same") for _ in range(3)]   # exact redeliveries
    stored += [post(conn, call, "ANSWERED") for _ in range(2)]                  # same fact, new ids
    assert stored == [True, False, False, True, True]
    process_batch(conn, "w")
    # Progressive: the first ANSWERED also connects the reserved agent, so the call is already
    # CONNECTED (higher rank) when the repeats are classified: STALE_IGNORED, a no-op either way.
    assert results(conn) == ["APPLIED", "STALE_IGNORED", "STALE_IGNORED"]
    assert transitions(conn, call).count("ANSWERED") == 1
    post(conn, call, "COMPLETED", duration_s=30)
    process_batch(conn, "w")
    assert transitions(conn, call) == ["RESERVED", "INITIATED", "ANSWERED", "CONNECTED", "COMPLETED"]


def test_duplicate_answered_while_waiting_for_agent(conn):
    call = _one_call(conn, mode="PREDICTIVE", agents=0)   # no agent: the call stays ANSWERED
    for _ in range(3):
        post(conn, call, "ANSWERED")
    process_batch(conn, "w")
    assert results(conn) == ["APPLIED", "DUPLICATE", "DUPLICATE"]
    assert transitions(conn, call) == ["QUEUED", "INITIATED", "ANSWERED"]


def test_out_of_order_completed_answered_ringing(conn):
    call = _one_call(conn)
    for etype, dur in (("COMPLETED", 42.0), ("ANSWERED", None), ("RINGING", None)):
        post(conn, call, etype, duration_s=dur)
    process_batch(conn, "w")
    assert results(conn) == ["APPLIED", "STALE_IGNORED", "STALE_IGNORED"]
    c = conn.execute("SELECT state, answered_inferred, duration_s FROM calls").fetchone()
    assert c == {"state": "COMPLETED", "answered_inferred": True, "duration_s": 42.0}
    # Answered (inferred) but we never bridged an agent: counted conservatively as an abandon.
    assert conn.execute("SELECT last_outcome FROM borrowers").fetchone()["last_outcome"] == "ABANDONED"
    assert conn.execute("SELECT state FROM agents").fetchone()["state"] == "AVAILABLE"


def _post_seq(conn, call_id, etype, seq, duration_s=None, age_ms=0):
    conn.execute(
        """INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, sequence, payload, received_at, next_attempt_at)
           VALUES ('A', %s, %s, %s, %s, %s, now() - %s * interval '1 millisecond', now() - %s * interval '1 millisecond')""",
        (uuid.uuid4().hex, call_id, etype, seq, json.dumps({"duration_s": duration_s}), age_ms, age_ms))


def test_resequencing_recovers_a_late_answered(conn):
    """Provider numbers events. COMPLETED (seq 3) arrives before ANSWERED (seq 2) and RINGING (seq 1)
    is lost. Within the hold window the gap fills: the call is CONNECTED then COMPLETED, no false abandon."""
    call = _one_call(conn)
    _post_seq(conn, call, "COMPLETED", 3, duration_s=50)
    process_batch(conn, "w")
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "INITIATED", "held: waiting for seq 2"
    _post_seq(conn, call, "ANSWERED", 2, age_ms=2000)   # arrived a while ago: ANSWERED's own hold has expired
    conn.execute("UPDATE provider_events SET next_attempt_at = now()")
    process_batch(conn, "w")
    assert transitions(conn, call)[-3:] == ["ANSWERED", "CONNECTED", "COMPLETED"]
    assert conn.execute("SELECT last_outcome FROM borrowers").fetchone()["last_outcome"] == "CONNECTED"


def test_resequencing_gives_up_after_window(conn):
    call = _one_call(conn)
    _post_seq(conn, call, "COMPLETED", 3, duration_s=50, age_ms=2000)
    process_batch(conn, "w")
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "COMPLETED"


def test_per_call_fifo_older_event_first(conn):
    call = _one_call(conn)
    post(conn, call, "ANSWERED")
    post(conn, call, "COMPLETED", duration_s=10)
    # make the newer event look due first: the per-call ordering must still apply ANSWERED first
    conn.execute("UPDATE provider_events SET next_attempt_at = now() - interval '1 hour' WHERE event_type = 'COMPLETED'")
    ev = process_one(conn, "w")
    assert ev["event_type"] == "ANSWERED"


def test_failure_after_answer_is_rejected_not_applied(conn):
    call = _one_call(conn)
    post(conn, call, "ANSWERED")
    post(conn, call, "NO_ANSWER")
    process_batch(conn, "w")
    assert results(conn) == ["APPLIED", "REJECTED"]
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "CONNECTED"


def test_unknown_call_becomes_orphan_after_retries(conn):
    post(conn, uuid.uuid4(), "ANSWERED")
    for _ in range(5):
        process_one(conn, "w")
        conn.execute("UPDATE provider_events SET next_attempt_at = now()")
    assert results(conn) == ["ORPHAN"]


def test_provider_cannot_forge_not_found(conn):
    call = _one_call(conn)
    post(conn, call, "NOT_FOUND")
    process_batch(conn, "w")
    assert results(conn) == ["REJECTED"]


LIFECYCLES = [["RINGING", "ANSWERED", "COMPLETED"], ["RINGING", "NO_ANSWER"], ["RINGING", "BUSY"], ["FAILED"]]


@pytest.mark.parametrize("seed", range(40))
def test_property_random_event_streams(conn, seed):
    """Duplicated, shuffled and dropped events, through the real inbox and processor, on both
    modes: always a terminal end state, never an invalid or duplicate transition, no stuck rows."""
    rng = random.Random(seed)
    mode = rng.choice(["PROGRESSIVE", "PREDICTIVE"])
    call = _one_call(conn, mode=mode)
    life = rng.choice(LIFECYCLES)
    events = [e for e in life[:-1] if rng.random() > 0.3] + [life[-1]]
    stream = []
    for e in events:
        eid = uuid.uuid4().hex
        for _ in range(rng.randint(1, 3)):
            stream.append((e, eid if rng.random() < 0.5 else uuid.uuid4().hex))
    rng.shuffle(stream)
    for e, eid in stream:
        post(conn, call, e, event_id=eid, duration_s=60.0 if e == "COMPLETED" else None)
    process_batch(conn, "w")
    state = conn.execute("SELECT state FROM calls").fetchone()["state"]
    assert state in ("COMPLETED", "FAILED"), (life, stream, state)
    assert all(n == 0 for _, n in check_all(conn)), check_all(conn)


def test_webhook_stores_once(dsn, conn):
    from fastapi.testclient import TestClient
    from smartdialer.events import webhook_api
    call = _one_call(conn)
    body = {"event_id": "e1", "type": "RINGING", "call_id": str(call), "provider_call_id": "x"}
    with TestClient(webhook_api.app) as client:
        assert client.post("/events/A", json=body).json() == {"stored": True, "duplicate": False}
        assert client.post("/events/A", json=body).json() == {"stored": False, "duplicate": True}
    assert conn.execute("SELECT count(*) AS n FROM provider_events").fetchone()["n"] == 1
