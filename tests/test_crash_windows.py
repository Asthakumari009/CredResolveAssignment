"""C3: both dispatch crash windows, with a real SIGKILL, resolved via get_call_status(idempotency key).

W1: crash after the dispatch transaction committed, before the provider was contacted.
W2: crash after the provider accepted the dial, before INITIATED was recorded.

Runs the real mock provider over HTTP, because the provider's memory must outlive the crashed
process (that is the whole point: the provider is the authority on whether the call exists).
"""
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from smartdialer.allocator.allocator import CallAllocator
from smartdialer.db import connect
from smartdialer.events import answer_hold
from smartdialer.events.processor import process_batch, process_one
from smartdialer.invariants import check_all
from smartdialer.providers.client import HttpProvider
from smartdialer.recovery import reaper
from smartdialer.safety.controller import SafetyController
from tests.helpers import issue_permit, make_campaign

MOCK = "http://127.0.0.1:18001"
CTX = mp.get_context("fork")


@pytest.fixture(scope="module")
def mock_provider():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_provider.app:app", "--port", "18001", "--log-level", "warning"],
                            env={**os.environ, "WEBHOOK_URL": "http://127.0.0.1:1/none"})
    for _ in range(100):
        try:
            httpx.get(MOCK + "/health", timeout=0.3)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield HttpProvider("A", MOCK)
    proc.terminate()
    proc.wait()


@pytest.fixture
def provider(mock_provider):
    httpx.post(MOCK + "/control/reset")
    httpx.post(MOCK + "/a/control", json={"auto_events": False, "error_rate": 0.0, "latency_ms": [1, 5]})
    return mock_provider


def _dispatch_and_crash(dsn, cid, crash_at):
    os.environ["SMARTDIALER_CRASH_AT"] = crash_at
    with connect(dsn) as conn:
        CallAllocator(SafetyController("doomed"), {"A": HttpProvider("A", MOCK)}, "doomed").dispatch(conn, cid, 1, 3)
    os._exit(0)   # not reached when the crash point fires


def _run_crashing(target, *args):
    p = CTX.Process(target=target, args=args)
    p.start()
    p.join(timeout=30)
    assert p.exitcode == -9, f"expected SIGKILL, got exit code {p.exitcode}"


def _state(conn):
    return {
        "call": conn.execute("SELECT state, failure_reason, provider_call_id FROM calls").fetchone(),
        "agent": (conn.execute("SELECT state FROM agents").fetchone() or {}).get("state"),
        "borrower": conn.execute("SELECT state, attempts FROM borrowers").fetchone(),
    }


def _reconcile(conn, provider):
    conn.execute("UPDATE campaigns SET settings = settings || %s", (json.dumps({"reconcile_pre_dial_s": 0}),))
    out = reaper.run(conn, {"A": provider}, "rescuer")
    process_batch(conn, "rescuer")
    return out


def test_w1_crash_before_dial(dsn, conn, provider):
    cid = make_campaign(conn, agents=1, borrowers=1)
    issue_permit(conn, cid, approved=1)
    _run_crashing(_dispatch_and_crash, dsn, cid, "after_commit_before_dial")

    s = _state(conn)
    assert s["call"]["state"] == "RESERVED" and s["agent"] == "RESERVED" and s["borrower"]["state"] == "IN_PROGRESS"
    call_id = str(conn.execute("SELECT id FROM calls").fetchone()["id"])
    assert provider.get_call_status(call_id).status == "NOT_FOUND"

    out = _reconcile(conn, provider)
    assert out["reconciled"] == [(call_id, "NOT_FOUND")]
    s = _state(conn)
    assert s["call"]["state"] == "CANCELLED" and s["call"]["failure_reason"] == "NOT_DIALED"
    assert s["agent"] == "AVAILABLE"
    assert s["borrower"] == {"state": "PENDING", "attempts": 0}, "never dialed: not an attempt"
    assert all(n == 0 for _, n in check_all(conn))


def test_w2_crash_after_provider_accepted(dsn, conn, provider):
    cid = make_campaign(conn, agents=1, borrowers=1)
    issue_permit(conn, cid, approved=1)
    _run_crashing(_dispatch_and_crash, dsn, cid, "after_dial_before_initiated")

    s = _state(conn)
    assert s["call"]["state"] == "RESERVED" and s["call"]["provider_call_id"] is None
    call_id = str(conn.execute("SELECT id FROM calls").fetchone()["id"])
    truth = provider.get_call_status(call_id)
    assert truth.status == "INITIATED", "the provider placed the call before we died"

    out = _reconcile(conn, provider)
    assert out["reconciled"] == [(call_id, "INITIATED")]
    s = _state(conn)
    assert s["call"]["state"] == "INITIATED" and s["call"]["provider_call_id"] == truth.provider_call_id, "adopted"
    assert s["agent"] == "DIALING"
    # Later the provider's real events arrive and the call finishes normally, still one call.
    for et, dur in (("ANSWERED", None), ("COMPLETED", 61.0)):
        conn.execute("INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, payload)"
                     " VALUES ('A', %s, %s, %s, %s)", (uuid.uuid4().hex, call_id, et, json.dumps({"duration_s": dur})))
        process_batch(conn, "rescuer")   # one at a time, as they would arrive from a live provider
    s = _state(conn)
    assert s["call"]["state"] == "COMPLETED" and s["agent"] == "WRAP_UP" and s["borrower"]["state"] == "DONE"
    assert conn.execute("SELECT count(*) AS n FROM calls").fetchone()["n"] == 1, "no redial"
    assert all(n == 0 for _, n in check_all(conn))


def _process_and_crash(dsn, crash_at):
    os.environ["SMARTDIALER_CRASH_AT"] = crash_at
    with connect(dsn) as conn:
        while process_one(conn, "doomed"):
            pass
    os._exit(0)


def _post(conn, call_id, etype, duration_s=None):
    conn.execute("INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, payload)"
                 " VALUES ('A', %s, %s, %s, %s)", (uuid.uuid4().hex, call_id, etype, json.dumps({"duration_s": duration_s})))


def _one_dialed_call(conn, provider, mode="PROGRESSIVE", agents=1):
    cid = make_campaign(conn, mode=mode, agents=agents, borrowers=1)
    issue_permit(conn, cid, approved=1, mode=mode)
    [c] = CallAllocator(SafetyController("w"), {"A": provider}, "w").dispatch(conn, cid, 1, 3)
    return c["id"]


def test_crash_while_applying_answered_rolls_back(dsn, conn, provider):
    """The worker dies inside the ANSWERED transaction, so no agent was bridged while the borrower
    was on the line. When another worker gets to it, COMPLETED is already in the inbox: the borrower
    was abandoned. It must be recorded as ABANDONED and counted, never as CONNECTED."""
    call = _one_dialed_call(conn, provider)
    _post(conn, call, "ANSWERED")
    _run_crashing(_process_and_crash, dsn, "event_answered_before_commit")
    ev = conn.execute("SELECT processed_at FROM provider_events").fetchone()
    assert ev["processed_at"] is None, "rolled back: the inbox row is still there to be processed"
    assert _state(conn)["call"]["state"] == "INITIATED"
    _post(conn, call, "COMPLETED", 33.0)
    process_batch(conn, "survivor")
    s = _state(conn)
    assert s["call"]["state"] == "ABANDONED" and s["agent"] == "AVAILABLE" and s["borrower"]["state"] == "DONE"
    assert [r["to_state"] for r in conn.execute("SELECT to_state FROM call_transitions ORDER BY id")] == \
        ["RESERVED", "INITIATED", "ANSWERED", "ABANDONED"]
    assert conn.execute("SELECT last_outcome FROM borrowers").fetchone()["last_outcome"] == "ABANDONED"
    assert all(n == 0 for _, n in check_all(conn))


def test_crash_after_answered_committed_predictive_no_agent(dsn, conn, provider):
    """ANSWERED committed (no agent free), worker dies. Another worker's answer-hold abandons it
    after max hold; the late COMPLETED is then STALE_IGNORED. Consistent, and counted."""
    call = _one_dialed_call(conn, provider, mode="PREDICTIVE", agents=0)
    _post(conn, call, "ANSWERED")
    _run_crashing(_process_and_crash, dsn, "event_answered_after_commit")
    assert _state(conn)["call"]["state"] == "ANSWERED"
    conn.execute("UPDATE campaigns SET settings = settings || '{\"max_answer_hold_ms\": 0}'")   # hold time is up
    out = answer_hold.run(conn, {"A": provider}, "survivor")
    assert out == {"connected": 0, "abandoned": 1}
    _post(conn, call, "COMPLETED", 5.0)
    process_batch(conn, "survivor")
    s = _state(conn)
    assert s["call"]["state"] == "ABANDONED" and s["borrower"]["state"] == "DONE"
    assert conn.execute("SELECT processing_result FROM provider_events ORDER BY id DESC LIMIT 1").fetchone()["processing_result"] == "STALE_IGNORED"
    assert all(n == 0 for _, n in check_all(conn))
