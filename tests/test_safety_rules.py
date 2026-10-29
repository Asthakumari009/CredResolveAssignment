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


def run(proposal, s, mem=None, cfg=CFG):
    return decide(proposal, s, cfg, mem or {})


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
    d, _ = run(pred(1000), snap(available=1000, unbound_in_flight=0))
    assert d.approved_calls == CFG.absolute_inflight_cap


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
