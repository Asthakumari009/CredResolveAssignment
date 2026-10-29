"""Simulation scenarios (assignment table A-D, plus the answer-rate shocks E and F)."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    answer_rate: Callable[[float], float]   # t (s) -> probability a dialed borrower answers
    talk_mean_s: Callable[[float], float]   # t (s) -> mean talk time
    duration_s: float = 3600
    agents: int = 50
    wrap_up_s: float = 15
    shock_at_s: float | None = None


def _steps(points):
    def f(t):
        v = points[0][1]
        for start, val in points:
            if t >= start:
                v = val
        return v
    return f


SCENARIOS = {
    "A": Scenario("A", "20% answer rate, 120 s talk", lambda t: 0.20, lambda t: 120),
    "B": Scenario("B", "50% answer rate, 90 s talk", lambda t: 0.50, lambda t: 90),
    "C": Scenario("C", "70% answer rate, 180 s talk", lambda t: 0.70, lambda t: 180),
    "D": Scenario("D", "changing: answer rate 30% -> 50% -> 15% -> 40% and talk 90 -> 150 -> 60 -> 120 s every 15 min",
                  _steps([(0, 0.30), (900, 0.50), (1800, 0.15), (2700, 0.40)]),
                  _steps([(0, 90), (900, 150), (1800, 60), (2700, 120)])),
    "E": Scenario("E", "shock up: answer rate jumps 20% -> 70% at 20 min (the dangerous direction)",
                  _steps([(0, 0.20), (1200, 0.70)]), lambda t: 120, duration_s=2400, shock_at_s=1200),
    "F": Scenario("F", "shock down: answer rate drops 70% -> 10% at 20 min",
                  _steps([(0, 0.70), (1200, 0.10)]), lambda t: 120, duration_s=2400, shock_at_s=1200),
}
