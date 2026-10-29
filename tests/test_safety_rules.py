"""Each Safety Controller rule in isolation (pure, no DB)."""
from dataclasses import replace
from datetime import time, timedelta

from smartdialer.config import CampaignSettings
from smartdialer.domain.safety_rules import decide
from smartdialer.domain.types import PacingProposal
from tests.test_pacing import NOW, snap

CFG = CampaignSettings()


def pred(n, at=NOW):
    return PacingProposal(n, "PREDICTIVE", "test", at)


# Rule memory of a campaign that has been dialing normally for a long time: no recovery ramp in
# progress. Tests of other rules start here; the ramp tests below pass their own memory.
STEADY = {"ramp_from": 0.0}


def run(proposal, s, mem=None, cfg=CFG):
    return decide(proposal, s, cfg, dict(STEADY) if mem is None else mem)


def test_healthy_predictive_is_approved():
    d, _ = run(pred(12), snap(available=10))
    assert (d.outcome, d.approved_calls, d.mode, d.reason_codes) == ("APPROVE", 12, "PREDICTIVE", ())


def test_not_pacing_leader():
    d, _ = run(pred(5), snap(leader_ok=False))
    assert (d.outcome, d.approved_calls, d.reason_codes) == ("REJECT", 0, ("NOT_PACING_LEADER",))


def test_stale_snapshot():
    d, _ = run(pred(5, at=NOW - timedelta(seconds=2)), snap())
    assert d.reason_codes == ("STALE_SNAPSHOT",) and d.approved_calls == 0


def test_outside_calling_window():
    cfg = replace(CFG, window_start="08:00", window_end="19:00")
    d, _ = run(pred(5), snap(local_time=time(19, 30)), cfg=cfg)
    assert d.reason_codes == ("OUTSIDE_CALLING_WINDOW",) and d.approved_calls == 0


def test_provider_open_rejects_half_open_probes_degraded_halves():
    assert run(pred(20), snap(breaker_state="OPEN"))[0].outcome == "REJECT"
    d, _ = run(pred(20), snap(breaker_state="HALF_OPEN", available=10))
    assert d.approved_calls == CFG.half_open_probes and "PROVIDER_HEALTH" in d.reason_codes
    d, _ = run(pred(20), snap(breaker_state="DEGRADED", available=10))
    assert d.approved_calls == 10 and "PROVIDER_HEALTH" in d.reason_codes


def test_cold_start_falls_back_to_progressive():
    d, _ = run(pred(30), snap(window_n=10, window_answered=3, available=6))
    assert (d.outcome, d.mode, d.approved_calls) == ("FALLBACK_TO_PROGRESSIVE", "PROGRESSIVE", 6)
    assert "COLD_START" in d.reason_codes


def test_hard_ratio_cap():
    d, _ = run(pred(100), snap(available=10, unbound_in_flight=5))
    assert d.approved_calls == 25 - 5 and d.outcome == "REDUCE" and d.reason_codes == ("HARD_RATIO_CAP",)
    # Absolute cap: 1,000 agents would allow 2,500, but only 500 may be in flight, and 480 already are.
    d, _ = run(pred(1000), snap(available=1000, unbound_in_flight=CFG.absolute_inflight_cap - 20))
    assert (d.approved_calls, d.reason_codes) == (20, ("HARD_RATIO_CAP",))


def test_abandon_budget_with_hysteresis():
    s_bad = snap(abandon_n=100, abandon_count=3)          # 3% -> trip
    d, mem = run(pred(10), s_bad)
    assert "ABANDON_BUDGET" in d.reason_codes and d.mode == "PROGRESSIVE"
    s_meh = snap(abandon_n=100, abandon_count=2)          # 2%: below trip, above resume -> stay tripped
    d, mem = run(pred(10), s_meh, mem)
    assert "ABANDON_BUDGET" in d.reason_codes
    s_ok = snap(abandon_n=100, abandon_count=1)           # 1% < 1.5% resume: start the clock
    d, mem = run(pred(10), s_ok, mem)
    assert "ABANDON_BUDGET" in d.reason_codes, "still within hysteresis"
    later = snap(abandon_n=100, abandon_count=1, taken_at=NOW + timedelta(seconds=61))
    d, mem = run(pred(10, at=later.taken_at), later, mem)
    assert "ABANDON_BUDGET" not in d.reason_codes and d.mode == "PREDICTIVE"


def test_agent_drop_cooldown():
    d, mem = run(pred(10), snap(staffed=60, available=20, max_staffed_recent=100))
    assert "AGENT_DROP" in d.reason_codes and d.mode == "PROGRESSIVE" and d.approved_calls == 20
    # agents back, but cooldown still running
    s = snap(staffed=100, max_staffed_recent=100, taken_at=NOW + timedelta(seconds=10))
    d, mem = run(pred(10, at=s.taken_at), s, mem)
    assert "AGENT_DROP" in d.reason_codes
    s = snap(staffed=100, max_staffed_recent=100, taken_at=NOW + timedelta(seconds=31))
    d, mem = run(pred(10, at=s.taken_at), s, mem)
    assert "AGENT_DROP" not in d.reason_codes


def test_small_dip_is_not_a_drop():
    d, _ = run(pred(10), snap(staffed=85, max_staffed_recent=100))
    assert "AGENT_DROP" not in d.reason_codes


def test_answer_rate_drift_both_directions():
    up = snap(window_n=200, window_answered=40, short_n=30, short_answered=21)     # 20% long, 70% short
    d, _ = run(pred(10), up)
    assert "ANSWER_RATE_DRIFT" in d.reason_codes and d.mode == "PROGRESSIVE"
    down = snap(window_n=200, window_answered=140, short_n=30, short_answered=3)   # 70% long, 10% short
    d, _ = run(pred(10), down)
    assert "ANSWER_RATE_DRIFT" in d.reason_codes


def test_answer_rate_noise_is_not_drift():
    # long 0.31, short 14/30 = 0.47: a 0.16 gap, but only ~1.9 standard errors at n=30
    d, _ = run(pred(10), snap(window_n=200, window_answered=62, short_n=30, short_answered=14))
    assert "ANSWER_RATE_DRIFT" not in d.reason_codes


def test_progressive_proposal_bounded_by_available():
    d, _ = run(PacingProposal(10, "PROGRESSIVE", "t", NOW), snap(available=4))
    assert (d.approved_calls, d.outcome) == (4, "REDUCE")


def test_pacer_cannot_switch_safety_off():
    """There is no field in a proposal that disables a rule: a huge predictive ask on a bad
    snapshot is still capped."""
    d, _ = run(pred(10_000), snap(available=2, abandon_n=100, abandon_count=10))
    assert d.approved_calls <= 2 and d.mode == "PROGRESSIVE"


def _at(sec):
    return NOW + timedelta(seconds=sec)


def _step(n, mem, sec, mode="PREDICTIVE", **kw):
    d, mem = decide(PacingProposal(n, mode, "test", _at(sec)), snap(taken_at=_at(sec), available=60, **kw), CFG, mem)
    return d, mem


def test_ramp_from_startup():
    """A campaign's first decisions ramp 20% -> 100% over 20 s, then release in full."""
    d, mem = _step(20, {}, 0)
    assert (d.outcome, d.approved_calls, d.reason_codes) == ("REDUCE", 4, ("RECOVERY_RAMP",))
    d, mem = _step(20, mem, 10)
    assert d.approved_calls == 12, "20 x (0.2 + 0.8 x 0.5)"
    d, mem = _step(20, mem, 20)
    assert (d.approved_calls, d.reason_codes) == (20, ())
    d, _ = _step(1, {}, 0)
    assert d.approved_calls == 1, "never ramps a nonzero approval down to zero"


def test_ramp_restarts_after_any_reject_that_paused_dialing():
    for pause in (dict(event_lag_ms=5000), dict(breaker_state="OPEN"), dict(local_time=time(20, 0))):
        d, mem = _step(20, dict(STEADY), 0, **pause)
        assert d.outcome == "REJECT" and mem["ramp_from"] is None
        d, mem = _step(20, mem, 1)
        assert (d.approved_calls, d.reason_codes) == (4, ("RECOVERY_RAMP",)), pause


def test_ramp_after_breaker_half_open_and_agent_drop():
    d, mem = _step(20, dict(STEADY), 0, breaker_state="HALF_OPEN")
    assert d.approved_calls == CFG.half_open_probes and mem["ramp_from"] is None, "probes only, no ramp yet"
    d, mem = _step(20, mem, 1)
    assert (d.approved_calls, d.reason_codes) == (4, ("RECOVERY_RAMP",)), "ramp starts when the breaker closes"

    d, mem = _step(20, dict(STEADY), 0, staffed=50, max_staffed_recent=100)
    assert "AGENT_DROP" in d.reason_codes and mem["ramp_from"] is None
    d, mem = _step(20, mem, CFG.agent_drop_cooldown_s + 1, staffed=50, max_staffed_recent=50)
    assert (d.mode, d.approved_calls, d.reason_codes) == ("PREDICTIVE", 4, ("RECOVERY_RAMP",)), "cooldown over: ramp"


def test_void_decisions_do_not_restart_the_ramp():
    """A stale or deposed-leader decision says nothing about whether dialing is safe."""
    d, mem = decide(PacingProposal(20, "PREDICTIVE", "test", _at(-5)), snap(), CFG, dict(STEADY))
    assert d.reason_codes == ("STALE_SNAPSHOT",) and mem["ramp_from"] == 0.0
    d, mem = _step(20, mem, 0, leader_ok=False)
    assert d.reason_codes == ("NOT_PACING_LEADER",) and mem["ramp_from"] == 0.0


def test_per_tick_cap_in_every_mode():
    """No decision approves more than max_approved_per_tick: predictive, progressive, COLD_START."""
    cap = CFG.max_approved_per_tick
    d, _ = run(pred(200), snap(available=200))
    assert (d.outcome, d.approved_calls, d.reason_codes) == ("REDUCE", cap, ("MAX_PER_TICK",))
    d, _ = run(PacingProposal(1000, "PROGRESSIVE", "test", NOW), snap(available=1000))
    assert (d.approved_calls, d.reason_codes) == (cap, ("MAX_PER_TICK",))
    d, _ = run(pred(1000), snap(available=1000, window_n=0, window_answered=0))
    assert (d.outcome, d.approved_calls) == ("FALLBACK_TO_PROGRESSIVE", cap)
    assert d.reason_codes == ("COLD_START", "MAX_PER_TICK")
    d, _ = run(pred(30), snap(available=30), cfg=replace(CFG, max_approved_per_tick=10))
    assert d.approved_calls == 10, "configurable"


def test_calling_window_uses_the_injected_clock(conn, monkeypatch):
    """Real 08:00-19:00 window, clock set explicitly: 13:00 IST dials, 20:00 IST is rejected."""
    from smartdialer.config import load_settings
    from smartdialer.safety.snapshot import load_snapshot
    from tests.helpers import make_campaign
    cid = make_campaign(conn, mode="PREDICTIVE", agents=5)   # default timezone Asia/Kolkata
    cfg = load_settings(conn, cid)
    assert (cfg.window_start, cfg.window_end) == ("08:00", "19:00")
    noon = load_snapshot(conn, cid, cfg, "w", None)
    assert noon.local_time == time(13, 0)
    monkeypatch.setenv("SMARTDIALER_NOW", "2026-10-29T20:00:00+05:30")
    night = load_snapshot(conn, cid, cfg, "w", None)
    assert night.local_time == time(20, 0)
    d, _ = run(pred(5, at=night.taken_at), replace(night, leader_ok=True), cfg=cfg)
    assert d.reason_codes == ("OUTSIDE_CALLING_WINDOW",) and d.approved_calls == 0


def test_event_lag_rejects_every_dial_progressive_included():
    """Events arriving later than the answer hold: any new call would be abandoned before we see
    its ANSWERED. REJECT outright, not a progressive fallback, for both modes."""
    late = snap(available=10, event_lag_ms=CFG.max_answer_hold_ms + 1)
    for proposal in (pred(12), PacingProposal(5, "PROGRESSIVE", "test", NOW)):
        d, _ = run(proposal, late)
        assert (d.outcome, d.approved_calls, d.reason_codes) == ("REJECT", 0, ("EVENT_LAG",))
    ok, _ = run(pred(12), snap(available=10, event_lag_ms=CFG.max_answer_hold_ms))
    assert ok.outcome == "APPROVE" and "EVENT_LAG" not in ok.reason_codes


def _event(conn, call_id, *, sent_ago_s, received_ago_s, processed):
    import uuid
    conn.execute(
        """INSERT INTO provider_events (provider_name, provider_event_id, call_id, event_type, provider_timestamp,
                                        received_at, processed_at, processing_result)
           VALUES ('A', %s, %s, 'RINGING', now() - %s * interval '1 second', now() - %s * interval '1 second',
                   CASE WHEN %s THEN now() END, CASE WHEN %s THEN 'APPLIED' END)""",
        (uuid.uuid4().hex, call_id, sent_ago_s, received_ago_s, processed, processed))


def _call(conn, cid):
    b = conn.execute("SELECT id FROM borrowers WHERE campaign_id = %s LIMIT 1", (cid,)).fetchone()["id"]
    return conn.execute("""INSERT INTO calls (id, campaign_id, borrower_id, mode, provider_name, state, state_rank)
                           VALUES (gen_random_uuid(), %s, %s, 'PROGRESSIVE', 'A', 'FAILED', 9) RETURNING id""", (cid, b)).fetchone()["id"]


def test_snapshot_measures_event_lag_from_the_inbox(conn):
    """Lag is provider time -> applied, p90 over the window, or the oldest unprocessed event."""
    from smartdialer.config import load_settings
    from smartdialer.safety.snapshot import load_snapshot
    from tests.helpers import make_campaign
    cid = make_campaign(conn, agents=1, borrowers=1)
    other = make_campaign(conn, agents=1, borrowers=1)
    cfg = load_settings(conn, cid)
    lag = lambda: load_snapshot(conn, cid, cfg, "w", None).event_lag_ms   # noqa: E731
    assert lag() == 0.0, "no events, no lag"
    call = _call(conn, cid)
    for _ in range(10):
        _event(conn, call, sent_ago_s=0.3, received_ago_s=0.2, processed=True)
    assert 250 < lag() < 1000
    _event(conn, _call(conn, other), sent_ago_s=60, received_ago_s=1, processed=False)
    assert lag() < 1000, "another campaign's backlog is not ours"
    for _ in range(3):   # 3 of 13 events delivered 8 s late, like the 32-worker load test
        _event(conn, call, sent_ago_s=8.3, received_ago_s=1, processed=True)
    assert lag() > 8000
    conn.execute("DELETE FROM provider_events WHERE provider_timestamp < now() - interval '5 seconds'")
    _event(conn, call, sent_ago_s=3, received_ago_s=3, processed=False)   # one event stuck, unapplied
    assert 2900 < lag() < 4000
    snap = load_snapshot(conn, cid, cfg, "w", None)
    d, _ = run(PacingProposal(1, "PROGRESSIVE", "test", snap.taken_at), replace(snap, leader_ok=True), cfg=cfg)
    assert d.reason_codes == ("EVENT_LAG",)
