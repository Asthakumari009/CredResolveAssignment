"""Value objects passed between pacing, safety and allocation. Pure data, no behaviour."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, time


@dataclass(frozen=True)
class Snapshot:
    """Everything the pacing engine and the Safety Controller look at, read in one go from the DB."""
    campaign_id: int
    taken_at: datetime
    local_time: time
    leader_ok: bool
    fencing_token: int
    breaker_state: str
    # agents
    available: int
    staffed: int                # logged in and not paused (any state but OFFLINE / PAUSED)
    wrap_up: int
    bound_agents: int
    # live calls
    pre_dial: int
    ringing: int
    unbound_in_flight: int      # agentless (predictive) calls not yet answered
    answered_waiting: int       # answered, waiting for an agent
    connected: int
    # answer rate windows (resolved dials only: answered, NO_ANSWER or BUSY)
    window_n: int
    window_answered: int
    short_n: int
    short_answered: int
    # abandons over the last K answered-and-resolved calls
    abandon_n: int
    abandon_count: int
    avg_setup_s: float | None
    avg_talk_s: float | None
    max_staffed_recent: int     # highest `staffed` seen in recent decisions (agent-drop rule)

    def to_json(self) -> dict:
        d = asdict(self)
        d["taken_at"] = self.taken_at.isoformat()
        d["local_time"] = self.local_time.isoformat()
        return d


@dataclass(frozen=True)
class PacingProposal:
    requested_calls: int
    mode: str                   # PROGRESSIVE or PREDICTIVE
    explanation: str
    snapshot_taken_at: datetime


@dataclass(frozen=True)
class SafetyDecision:
    approved_calls: int
    mode: str                   # mode of the calls the permit allows
    outcome: str                # APPROVE, REDUCE, REJECT, FALLBACK_TO_PROGRESSIVE
    reason_codes: tuple[str, ...] = ()
    permit_id: int | None = None
    decision_id: int | None = None
    notes: tuple[str, ...] = field(default=(), compare=False)
