from datetime import datetime, time, timezone

import pytest

from smartdialer.config import CampaignSettings
from smartdialer.domain.stats import binom_tail_above, wilson_upper
from smartdialer.domain.types import Snapshot
from smartdialer.pacing import predictive, progressive

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)


def snap(**kw) -> Snapshot:
    base = dict(campaign_id=1, taken_at=NOW, local_time=time(10, 0), leader_ok=True, fencing_token=1,
                breaker_state="CLOSED", available=10, staffed=10, wrap_up=0, bound_agents=0, pre_dial=0, ringing=0,
                unbound_in_flight=0, answered_waiting=0, connected=0, window_n=400, window_answered=124,
                short_n=30, short_answered=9, abandon_n=100, abandon_count=0, avg_setup_s=8.0, avg_talk_s=120.0,
                max_staffed_recent=10)
    base.update(kw)
    return Snapshot(**base)


def test_wilson_known_value():
    assert wilson_upper(5, 10) == pytest.approx(0.7634, abs=1e-4)
    assert wilson_upper(0, 0) == 1.0, "no data: assume the worst"
    assert wilson_upper(124, 400) > 124 / 400


def test_binomial_tail_known_values():
    assert binom_tail_above(8, 10, 0.5) == pytest.approx(11 / 1024)
    assert binom_tail_above(5, 10, 0.5) == pytest.approx(386 / 1024)
    assert binom_tail_above(10, 10, 0.5) == 0.0
    assert binom_tail_above(-1, 10, 0.5) == 1.0


def test_proposal_is_the_largest_n_under_target():
    cfg = CampaignSettings()
    s = snap(available=10, wrap_up=3, unbound_in_flight=3)
    prop = predictive.propose(s, cfg)
    n = prop.requested_calls
    p_hi = wilson_upper(124, 400)
    C = 10 + 1   # floor(0.5 * 3) = 1 soon-free agent
    assert binom_tail_above(C, 3 + n, p_hi) <= cfg.target_overflow < binom_tail_above(C, 3 + n + 1, p_hi)
    assert n > 10, "with p ~ 0.31 predictive dials well above 1:1"
    for token in ("available=10", "ringing=3", "p=0.31 (n=400)", f"p_hi={p_hi:.2f}", "C=11", "target=1.0%", f"Proposing {n}"):
        assert token in prop.explanation, (token, prop.explanation)


def test_higher_answer_rate_means_fewer_calls():
    cfg = CampaignSettings()
    low = predictive.propose(snap(window_answered=80), cfg).requested_calls     # p = 0.20
    high = predictive.propose(snap(window_answered=280), cfg).requested_calls   # p = 0.70
    assert low > high > 0


def test_uncertainty_makes_pacing_cautious():
    cfg = CampaignSettings()
    lots = predictive.propose(snap(window_n=1000, window_answered=310), cfg).requested_calls
    few = predictive.propose(snap(window_n=20, window_answered=6), cfg).requested_calls
    assert few < lots, "same p=0.31, but a wide interval pushes p_hi up"


def test_no_capacity_no_calls():
    cfg = CampaignSettings()
    assert predictive.propose(snap(available=0), cfg).requested_calls == 0
    assert predictive.propose(snap(available=3, answered_waiting=3), cfg).requested_calls == 0


def test_ringing_calls_count_against_capacity():
    cfg = CampaignSettings()
    assert predictive.propose(snap(unbound_in_flight=20), cfg).requested_calls < predictive.propose(snap(), cfg).requested_calls


def test_progressive_proposes_available():
    p = progressive.propose(snap(available=7))
    assert (p.requested_calls, p.mode) == (7, "PROGRESSIVE")
