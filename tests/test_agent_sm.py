import itertools

import pytest

from smartdialer.domain.agent_sm import ALLOWED, AgentState, can_transition

EXPECTED_ALLOWED = {
    ("OFFLINE", "AVAILABLE"),
    ("AVAILABLE", "RESERVED"), ("AVAILABLE", "CONNECTED"), ("AVAILABLE", "PAUSED"), ("AVAILABLE", "OFFLINE"),
    ("RESERVED", "DIALING"), ("RESERVED", "CONNECTED"), ("RESERVED", "AVAILABLE"),
    ("RESERVED", "PAUSED"), ("RESERVED", "OFFLINE"),
    ("DIALING", "CONNECTED"), ("DIALING", "AVAILABLE"), ("DIALING", "PAUSED"), ("DIALING", "OFFLINE"),
    ("CONNECTED", "WRAP_UP"), ("CONNECTED", "OFFLINE"),
    ("WRAP_UP", "AVAILABLE"), ("WRAP_UP", "PAUSED"), ("WRAP_UP", "OFFLINE"),
    ("PAUSED", "AVAILABLE"), ("PAUSED", "OFFLINE"),
}


def test_table_matches_expected():
    assert {(a.value, b.value) for a, b in ALLOWED} == EXPECTED_ALLOWED


@pytest.mark.parametrize("frm,to", list(itertools.product([s.value for s in AgentState], repeat=2)))
def test_every_pair(frm, to):
    assert can_transition(frm, to) == ((frm, to) in EXPECTED_ALLOWED)


def test_offline_agent_cannot_be_reserved():
    assert not can_transition("OFFLINE", "RESERVED")
    assert not can_transition("PAUSED", "RESERVED")
    assert not can_transition("WRAP_UP", "RESERVED")
