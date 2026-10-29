"""C2: reserved_call_id fences every worker-driven agent transition after reservation.

Setup for each case: a stale worker reserved the agent for call OLD. The agent was then released
and re-reserved for call NEW (and moved to the state under test). The stale worker now tries to
drive the agent with OLD. Every attempt must change nothing and log nothing.
"""
import uuid

import pytest

from smartdialer.transitions import reserve_agent, set_agent_state
from tests.helpers import make_campaign


def _agent(conn, aid):
    return conn.execute("SELECT state, reserved_call_id FROM agents WHERE id = %s", (aid,)).fetchone()


def _setup(conn, current_state):
    cid = make_campaign(conn, agents=1)
    old, new = uuid.uuid4(), uuid.uuid4()
    aid = reserve_agent(conn, cid, old, "stale-worker")
    assert set_agent_state(conn, aid, "AVAILABLE", fence_call_id=old, worker_id="stale-worker", reason="released")
    assert reserve_agent(conn, cid, new, "fresh-worker") == aid
    path = {"RESERVED": [], "DIALING": ["DIALING"], "CONNECTED": ["DIALING", "CONNECTED"],
            "WRAP_UP": ["DIALING", "CONNECTED", "WRAP_UP"]}[current_state]
    for s in path:
        assert set_agent_state(conn, aid, s, fence_call_id=new, worker_id="fresh-worker", reason="setup")
    return aid, old, new


@pytest.mark.parametrize("current,attempt", [
    ("RESERVED", "DIALING"),
    ("RESERVED", "CONNECTED"),
    ("RESERVED", "AVAILABLE"),     # stale release
    ("RESERVED", "PAUSED"),        # stale "agent lost during setup" handling
    ("DIALING", "CONNECTED"),
    ("DIALING", "AVAILABLE"),
    ("DIALING", "PAUSED"),
    ("CONNECTED", "WRAP_UP"),
    ("CONNECTED", "OFFLINE"),
    ("WRAP_UP", "AVAILABLE"),
])
def test_stale_worker_cannot_move_agent(conn, current, attempt):
    aid, old, new = _setup(conn, current)
    before = conn.execute("SELECT count(*) AS n FROM agent_transitions").fetchone()["n"]
    assert not set_agent_state(conn, aid, attempt, fence_call_id=old, worker_id="stale-worker", reason="stale")
    assert _agent(conn, aid) == {"state": current, "reserved_call_id": new}
    assert conn.execute("SELECT count(*) AS n FROM agent_transitions").fetchone()["n"] == before


def test_fresh_worker_still_can(conn):
    aid, old, new = _setup(conn, "RESERVED")
    assert set_agent_state(conn, aid, "DIALING", fence_call_id=new, worker_id="fresh-worker", reason="ok")
    assert _agent(conn, aid)["state"] == "DIALING"


def test_release_clears_fence(conn):
    aid, old, new = _setup(conn, "DIALING")
    assert set_agent_state(conn, aid, "AVAILABLE", fence_call_id=new, worker_id="fresh-worker", reason="no answer")
    assert _agent(conn, aid) == {"state": "AVAILABLE", "reserved_call_id": None}
    # and the now-released call id can no longer drive the agent either
    assert not set_agent_state(conn, aid, "PAUSED", fence_call_id=new, worker_id="fresh-worker", reason="late")
