"""Agent disappears during call setup, and reconciliation edge cases (no HTTP)."""
import json
import uuid

from smartdialer.agent_actions import agent_action
from smartdialer.allocator.allocator import CallAllocator
from smartdialer.events import answer_hold
from smartdialer.events.processor import process_batch
from smartdialer.invariants import check_all
from smartdialer.providers.interface import CallStatus, ProviderUnavailable
from smartdialer.recovery import reaper
from smartdialer.safety.controller import SafetyController
from tests.helpers import FakeProvider, issue_permit, make_campaign


def _dial(conn, provider, agents=1, mode="PROGRESSIVE"):
    cid = make_campaign(conn, mode=mode, agents=agents, borrowers=1)
    issue_permit(conn, cid, approved=1, mode=mode)
    [c] = CallAllocator(SafetyController("w"), {"A": provider}, "w").dispatch(conn, cid, 1, 3)
    return cid, c


def _post(conn, call_id, etype, duration_s=None):
    conn.execute("INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, payload)"
                 " VALUES ('A', %s, %s, %s, %s)", (uuid.uuid4().hex, call_id, etype, json.dumps({"duration_s": duration_s})))


def test_agent_pauses_before_answer_call_is_cancelled(conn):
    fake = FakeProvider()
    _, c = _dial(conn, fake)
    assert agent_action(conn, c["agent_id"], "PAUSED", "went to lunch")
    out = reaper.run(conn, {"A": fake}, "w2")
    assert out["agent_lost"] == [str(c["id"])]
    call = conn.execute("SELECT state, failure_reason, provider_call_id FROM calls").fetchone()
    assert call["state"] == "CANCELLED" and call["failure_reason"] == "AGENT_LOST"
    assert fake.hangups == [call["provider_call_id"]], "cancelled at the provider too"
    assert conn.execute("SELECT state FROM agents").fetchone()["state"] == "PAUSED"
    assert conn.execute("SELECT state FROM borrowers").fetchone()["state"] == "PENDING"
    # the provider's late events change nothing
    _post(conn, c["id"], "ANSWERED")
    process_batch(conn, "w2")
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "CANCELLED"
    assert all(n == 0 for _, n in check_all(conn))


def test_agent_leaves_after_answer_call_is_handed_off(conn):
    fake = FakeProvider()
    cid, c = _dial(conn, fake, agents=2)
    conn.execute("UPDATE agents SET state = 'PAUSED' WHERE reserved_call_id IS NULL")   # second agent away for now
    agent_action(conn, c["agent_id"], "OFFLINE", "connection dropped")
    # ANSWERED arrives: the reserved agent is gone, nobody else is free, the call waits
    _post(conn, c["id"], "ANSWERED")
    process_batch(conn, "w")
    assert conn.execute("SELECT state FROM calls").fetchone()["state"] == "ANSWERED"
    other = conn.execute("SELECT id FROM agents WHERE id <> %s", (c["agent_id"],)).fetchone()["id"]
    agent_action(conn, other, "AVAILABLE", "back")
    assert answer_hold.run(conn, {"A": fake}, "w") == {"connected": 1, "abandoned": 0}
    call = conn.execute("SELECT state, agent_id FROM calls").fetchone()
    assert call == {"state": "CONNECTED", "agent_id": other}


def test_abandon_after_hold_when_nobody_comes(conn):
    fake = FakeProvider()
    cid, c = _dial(conn, fake, agents=0, mode="PREDICTIVE")
    _post(conn, c["id"], "ANSWERED")
    process_batch(conn, "w")
    conn.execute("UPDATE campaigns SET settings = settings || '{\"max_answer_hold_ms\": 0}'")   # hold time is up
    assert answer_hold.run(conn, {"A": fake}, "w") == {"connected": 0, "abandoned": 1}
    assert conn.execute("SELECT state, failure_reason FROM calls").fetchone() == {"state": "ABANDONED", "failure_reason": "NO_AGENT_IN_TIME"}
    assert len(fake.hangups) == 1, "polite message + hangup"
    assert conn.execute("SELECT last_outcome FROM borrowers").fetchone()["last_outcome"] == "ABANDONED"


class DownProvider(FakeProvider):
    def get_call_status(self, key):
        raise ProviderUnavailable("timeout")


def test_provider_unreachable_keeps_trying_then_gives_up(conn):
    down = DownProvider()
    cid, c = _dial(conn, FakeProvider(lambda k: __import__("smartdialer.providers.interface", fromlist=["x"]).DialUncertain("t")))
    conn.execute("UPDATE campaigns SET settings = settings || '{\"reconcile_pre_dial_s\": 0, \"reconcile_give_up_s\": 3600}'")
    assert reaper.run(conn, {"A": down}, "w")["reconciled"] == [], "unknown: keep the reservation, retry later"
    conn.execute("UPDATE calls SET last_reconciled_at = NULL")
    conn.execute("UPDATE campaigns SET settings = settings || '{\"reconcile_give_up_s\": 0}'")
    assert reaper.run(conn, {"A": down}, "w")["reconciled"] == [(str(c["id"]), "TIMEOUT")]
    process_batch(conn, "w")
    assert conn.execute("SELECT state, failure_reason FROM calls").fetchone() == {"state": "FAILED", "failure_reason": "TIMEOUT"}
    assert conn.execute("SELECT state FROM agents").fetchone()["state"] == "AVAILABLE"


def test_status_poll_fills_in_missing_events(conn):
    class Truth(FakeProvider):
        def get_call_status(self, key):
            return CallStatus("COMPLETED", self.calls[key], 88.0)
    p = Truth()
    cid, c = _dial(conn, p)
    conn.execute("UPDATE campaigns SET settings = settings || '{\"reconcile_in_flight_s\": 0}'")
    reaper.run(conn, {"A": p}, "w")
    process_batch(conn, "w")
    call = conn.execute("SELECT state, duration_s, answered_inferred FROM calls").fetchone()
    assert call == {"state": "COMPLETED", "duration_s": 88.0, "answered_inferred": True}
