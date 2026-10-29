"""Per-provider circuit breaker, as a pure transition function.

    CLOSED    -> DEGRADED   failure rate over recent dials >= degraded_rate (Safety Controller reduces)
    CLOSED/DEGRADED -> OPEN failure rate >= open_rate (with at least min_calls dials): nothing dials
    OPEN      -> HALF_OPEN  after open_s
    HALF_OPEN -> CLOSED     `probes` probe dials succeeded
    HALF_OPEN -> OPEN       any probe failed
    DEGRADED  -> CLOSED     failure rate back under degraded_rate

A failure is a dial that timed out, was uncertain, or errored. Busy / no answer are not
provider failures.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BreakerConfig:
    min_calls: int = 10
    open_rate: float = 0.5
    degraded_rate: float = 0.2
    open_s: float = 10.0
    probes: int = 3


def next_state(state: str, since_s: float, now_s: float, recent_ok: int, recent_fail: int,
               probe_ok: int, probe_fail: int, cfg: BreakerConfig = BreakerConfig()) -> str:
    total = recent_ok + recent_fail
    rate = recent_fail / total if total else 0.0
    if state in ("CLOSED", "DEGRADED"):
        if total >= cfg.min_calls and rate >= cfg.open_rate:
            return "OPEN"
        if total >= cfg.min_calls and rate >= cfg.degraded_rate:
            return "DEGRADED"
        return "CLOSED"
    if state == "OPEN":
        return "HALF_OPEN" if now_s - since_s >= cfg.open_s else "OPEN"
    if state == "HALF_OPEN":
        if probe_fail:
            return "OPEN"
        return "CLOSED" if probe_ok >= cfg.probes else "HALF_OPEN"
    raise ValueError(state)
