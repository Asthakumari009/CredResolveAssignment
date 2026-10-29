"""Agent state machine: an explicit allowed-transitions table. Anything not listed is rejected."""
from enum import StrEnum


class AgentState(StrEnum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # held for one specific progressive call (agents.reserved_call_id)
    DIALING = "DIALING"        # the provider accepted the dial for that call
    CONNECTED = "CONNECTED"    # talking to a borrower
    WRAP_UP = "WRAP_UP"        # after-call work, not dialable
    PAUSED = "PAUSED"


S = AgentState

ALLOWED: frozenset[tuple[AgentState, AgentState]] = frozenset({
    (S.OFFLINE, S.AVAILABLE),        # agent logs in
    (S.AVAILABLE, S.RESERVED),       # progressive allocator wins the atomic reserve
    (S.AVAILABLE, S.CONNECTED),      # claimed for an already-answered call (predictive, or hand-off)
    (S.AVAILABLE, S.PAUSED),
    (S.AVAILABLE, S.OFFLINE),
    (S.RESERVED, S.DIALING),         # provider accepted the dial
    (S.RESERVED, S.CONNECTED),       # answer arrived before the dial response was recorded
    (S.RESERVED, S.AVAILABLE),       # call failed / cancelled before it was dialed
    (S.RESERVED, S.PAUSED),          # agent disappears during setup
    (S.RESERVED, S.OFFLINE),
    (S.DIALING, S.CONNECTED),        # borrower answered
    (S.DIALING, S.AVAILABLE),        # no answer / busy / failed / cancelled
    (S.DIALING, S.PAUSED),           # agent disappears during setup
    (S.DIALING, S.OFFLINE),
    (S.CONNECTED, S.WRAP_UP),        # call ended
    (S.CONNECTED, S.OFFLINE),        # agent dropped mid-call; the call still runs to completion
    (S.WRAP_UP, S.AVAILABLE),        # agent finished notes
    (S.WRAP_UP, S.PAUSED),
    (S.WRAP_UP, S.OFFLINE),
    (S.PAUSED, S.AVAILABLE),
    (S.PAUSED, S.OFFLINE),
})

# States in which the agent is bound to a call through reserved_call_id.
BOUND = frozenset({S.RESERVED, S.DIALING, S.CONNECTED})


def can_transition(frm: str, to: str) -> bool:
    return (AgentState(frm), AgentState(to)) in ALLOWED
