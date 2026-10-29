"""Demo: the answer rate jumps 20% -> 70% (dangerous) and, separately, drops 70% -> 10% (safe
direction). Runs in the simulator (virtual clock, same pacing and Safety Controller code as the
live workers) so the shock is exact and repeatable. Prints the Safety Controller's decisions
around each shock and checks the abandon outcome.
"""
import sys

from sim.scenarios import SCENARIOS
from sim.simulator import Sim


def analyse(name: str) -> dict:
    sim = Sim(SCENARIOS[name], "PREDICTIVE", "A", seed=7).run()
    shock = sim.sc.shock_at_s
    print(f"\n=== Scenario {name}: {sim.sc.description} ===")
    print(f"  {'t-shock':>7} {'p_short':>7} {'p_long':>6} {'avail':>5} {'ringing':>7} {'asked':>5} {'ok':>4}  outcome / mode / reasons")
    last = None
    for d in sim.decisions:
        key = (d["outcome"], d["mode"], d["reasons"])
        if key == last or d["t"] < shock - 20:
            continue
        last = key
        fmt = lambda v: f"{v:.2f}" if v is not None else "-"
        print(f"  {d['t'] - shock:+7.0f} {fmt(d['p_short']):>7} {fmt(d['p_long']):>6} {d['available']:5} {d['in_flight']:7}"
              f" {d['requested']:5} {d['approved']:4}  {d['outcome']} / {d['mode']} / {d['reasons'] or '-'}")
    after = [d for d in sim.decisions if d["t"] >= shock]
    first_guard = next((d["t"] - shock for d in after if d["mode"] == "PROGRESSIVE" or "HARD_RATIO_CAP" in d["reasons"]), None)
    first_drift = next((d["t"] - shock for d in after if "ANSWER_RATE_DRIFT" in d["reasons"]), None)
    m0, m1 = int(shock // 60), int(shock // 60) + 5
    window = sim.buckets[m0:m1]
    ab, an = sum(b.abandoned for b in window), sum(b.answered for b in window)
    print(f"\n  first ratio cap or fallback after the shock: {first_guard} s; first ANSWER_RATE_DRIFT: {first_drift} s")
    print(f"  abandons in the 5 minutes after the shock: {ab} of {an} answered ({ab / an if an else 0:.1%})")
    return {"first_drift_s": first_drift, "abandons": ab, "answered": an, "summary": sim.summary()}


def main():
    up = analyse("E")
    down = analyse("F")
    ok = (up["first_drift_s"] is not None and up["first_drift_s"] <= 120 and up["abandons"] / max(1, up["answered"]) < 0.05
          and down["first_drift_s"] is not None and down["abandons"] == 0)
    print("\n  20% -> 70%: dangerous. p_hi lags the jump, so for a short while pacing still plans for 20%. What limits the")
    print("  damage: HARD_RATIO_CAP (in-flight <= 2.5 x agents available NOW), then ANSWER_RATE_DRIFT forcing progressive,")
    print("  then ABANDON_BUDGET if abandons pile up. Abandons in that window are counted, not assumed away.")
    print("  70% -> 10%: no compliance risk; agents idle. Drift still forces progressive briefly, the window relearns p,")
    print("  and predictive resumes with a much higher dial ratio.")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
