"""Simulator: deterministic, uses the shared pure logic, and the answer-rate shock (failure scenario 7)."""
from demos.answer_rate_shock import analyse
from sim.scenarios import SCENARIOS
from sim.simulator import Sim


def test_same_seed_same_result():
    a = Sim(SCENARIOS["B"], "PREDICTIVE", "A", seed=3).run().summary()
    b = Sim(SCENARIOS["B"], "PREDICTIVE", "A", seed=3).run().summary()
    assert a == b


def test_predictive_beats_progressive_within_budget_scenario_a():
    prog = Sim(SCENARIOS["A"], "PROGRESSIVE", "A", seed=7).run().summary()
    pred = Sim(SCENARIOS["A"], "PREDICTIVE", "A", seed=7).run().summary()
    assert pred["utilization"] > prog["utilization"] * 1.2
    assert pred["abandon_rate"] < 0.03
    assert prog["abandoned"] == 0, "progressive with a reliable provider never abandons"


def test_answer_rate_shock_up_is_contained():
    r = analyse("E")
    assert r["first_drift_s"] is not None and r["first_drift_s"] <= 120
    assert r["abandons"] / r["answered"] < 0.05


def test_answer_rate_shock_down_is_safe():
    r = analyse("F")
    assert r["first_drift_s"] is not None
    assert r["abandons"] == 0
