"""Safety Controller rules. Pure: decide(proposal, snapshot, settings, memory) -> (decision, memory).

The rules do not depend on the prediction being right. They look at hard facts in the snapshot
(how many agents are actually AVAILABLE right now, how many unanswered calls are in flight,
recent abandons, provider health) and cap what the pacing engine asked for.

Order matters only for readability: every rule that fires is recorded, and the strictest
effect wins (REJECT > FALLBACK_TO_PROGRESSIVE > REDUCE > APPROVE).

`memory` is the campaign's safety_state (jsonb): timestamps for cooldowns and the abandon
hysteresis flag. Times are DB-clock epoch seconds taken from snapshot.taken_at.
"""
import math

from smartdialer.config import CampaignSettings
from smartdialer.domain.calling_window import is_open
from smartdialer.domain.types import PacingProposal, SafetyDecision, Snapshot

PROGRESSIVE, PREDICTIVE = "PROGRESSIVE", "PREDICTIVE"


def progressive_budget(snap: Snapshot) -> int:
    """Progressive never needs a prediction: one call per agent that is AVAILABLE right now.
    The allocator also reserves a specific agent per call, so this is a ceiling, not a promise."""
    return max(0, snap.available)


def decide(proposal: PacingProposal, snap: Snapshot, cfg: CampaignSettings, memory: dict) -> tuple[SafetyDecision, dict]:
    mem = dict(memory or {})
    now = snap.taken_at.timestamp()
    notes: list[str] = []

    def reject(code: str, note: str):
        return SafetyDecision(0, proposal.mode, "REJECT", (code,), notes=(note,)), mem

    # 8. NOT_PACING_LEADER: a deposed leader's opinion is worthless.
    if not snap.leader_ok:
        return reject("NOT_PACING_LEADER", "fencing token or pacing lease does not match")
    # 7. STALE_SNAPSHOT: the pacer computed on data that is too old.
    age_ms = (snap.taken_at - proposal.snapshot_taken_at).total_seconds() * 1000
    if age_ms > cfg.stale_snapshot_ms:
        return reject("STALE_SNAPSHOT", f"proposal based on data {age_ms:.0f} ms old > {cfg.stale_snapshot_ms} ms")
    # Compliance: never dial outside the calling window.
    if not is_open(snap.local_time, cfg.window_start, cfg.window_end):
        return reject("OUTSIDE_CALLING_WINDOW", f"local time {snap.local_time} outside {cfg.window_start}-{cfg.window_end}")
    # 6. PROVIDER_HEALTH: circuit open, nothing goes out.
    if snap.breaker_state == "OPEN":
        return reject("PROVIDER_HEALTH", "circuit breaker OPEN")

    reasons: list[str] = []
    mode = proposal.mode
    outcome = "APPROVE"

    if proposal.mode == PREDICTIVE:
        fallback = _fallback_reasons(snap, cfg, mem, now, notes)
        if fallback:
            reasons += fallback
            mode, outcome = PROGRESSIVE, "FALLBACK_TO_PROGRESSIVE"

    if mode == PROGRESSIVE:
        budget = progressive_budget(snap)
        requested = proposal.requested_calls if proposal.mode == PROGRESSIVE else budget
        approved = min(requested, budget)
        if approved < proposal.requested_calls and outcome == "APPROVE":
            outcome = "REDUCE"
            reasons.append("PROGRESSIVE_BUDGET")
            notes.append(f"progressive: available={snap.available}")
    else:
        approved = proposal.requested_calls
        # 2. HARD_RATIO_CAP: unanswered in-flight calls <= ratio x agents AVAILABLE now, and an absolute cap.
        ratio_cap = math.floor(cfg.max_overdial_ratio * snap.available) - snap.unbound_in_flight
        abs_cap = cfg.absolute_inflight_cap - snap.unbound_in_flight
        cap = max(0, min(ratio_cap, abs_cap))
        if approved > cap:
            reasons.append("HARD_RATIO_CAP")
            notes.append(f"cap = min({cfg.max_overdial_ratio} x {snap.available} available, {cfg.absolute_inflight_cap})"
                         f" - {snap.unbound_in_flight} in flight = {cap}")
            approved, outcome = cap, "REDUCE"

    # 6. PROVIDER_HEALTH (partial): HALF_OPEN lets only a few probe calls through.
    if snap.breaker_state == "HALF_OPEN" and approved > cfg.half_open_probes:
        reasons.append("PROVIDER_HEALTH")
        notes.append(f"breaker HALF_OPEN: at most {cfg.half_open_probes} probe calls")
        approved = cfg.half_open_probes
        outcome = outcome if outcome == "FALLBACK_TO_PROGRESSIVE" else "REDUCE"

    return SafetyDecision(approved, mode, outcome, tuple(reasons), notes=tuple(notes)), mem


def _fallback_reasons(snap: Snapshot, cfg: CampaignSettings, mem: dict, now: float, notes: list[str]) -> list[str]:
    reasons = []
    # 1. COLD_START: not enough evidence to estimate an answer rate.
    if snap.window_n < cfg.cold_start_samples:
        reasons.append("COLD_START")
        notes.append(f"only {snap.window_n} resolved dials < {cfg.cold_start_samples}")

    # 3. ABANDON_BUDGET with hysteresis: trip at >= threshold, untrip only after the rate has
    # stayed below `resume` for abandon_hysteresis_s. Prevents flapping around the threshold.
    rate = snap.abandon_count / snap.abandon_n if snap.abandon_n else 0.0
    if snap.abandon_n >= cfg.abandon_min_samples and rate >= cfg.abandon_threshold:
        mem["abandon_tripped"], mem["abandon_ok_since"] = True, None
    elif mem.get("abandon_tripped"):
        if rate < cfg.abandon_resume:
            mem["abandon_ok_since"] = mem.get("abandon_ok_since") or now
            if now - mem["abandon_ok_since"] >= cfg.abandon_hysteresis_s:
                mem["abandon_tripped"], mem["abandon_ok_since"] = False, None
        else:
            mem["abandon_ok_since"] = None
    if mem.get("abandon_tripped"):
        reasons.append("ABANDON_BUDGET")
        notes.append(f"abandon rate {rate:.1%} over last {snap.abandon_n} answered (trip {cfg.abandon_threshold:.0%},"
                     f" resume below {cfg.abandon_resume:.1%} for {cfg.abandon_hysteresis_s:.0f}s)")

    # 4. AGENT_DROP: available agents fell sharply vs the recent max: stop overdialing for a cooldown.
    if snap.max_available_recent and snap.available < (1 - cfg.agent_drop_pct) * snap.max_available_recent:
        mem["agent_drop_until"] = now + cfg.agent_drop_cooldown_s
        notes.append(f"available {snap.available} < {1 - cfg.agent_drop_pct:.0%} of recent max {snap.max_available_recent}")
    if now < (mem.get("agent_drop_until") or 0):
        reasons.append("AGENT_DROP")

    # 5. ANSWER_RATE_DRIFT: recent answer rate disagrees with the long window, so the estimate is stale.
    if snap.short_n >= cfg.short_window and snap.window_n:
        p_short = snap.short_answered / snap.short_n
        p_long = snap.window_answered / snap.window_n
        if abs(p_short - p_long) > cfg.drift_threshold:
            mem["drift_until"] = now + cfg.drift_cooldown_s
            notes.append(f"short-window p={p_short:.2f} vs long-window p={p_long:.2f}")
    if now < (mem.get("drift_until") or 0):
        reasons.append("ANSWER_RATE_DRIFT")
    return reasons
