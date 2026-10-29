"""Predictive pacing: statistical, explainable, proposal only.

Question it answers: how many MORE agentless calls can we start now, so that the chance of
more borrowers answering than we have agents for stays below target_overflow (default 1%)?

    p      = answered / resolved dials in the sliding window (last answer_window)
    p_hi   = Wilson 95% upper bound of p. Plan for the high end: more answers is the dangerous direction.
    C      = available + floor(soon_free_discount x agents in WRAP_UP) - answered calls already waiting
    R      = agentless calls already in flight and not yet answered
    n      = largest n with P(Binomial(R + n, p_hi) > C) <= target_overflow

Known simplification (conservative): it assumes every in-flight call resolves before any
agent frees up, and treats calls as independent coin flips.
"""
import math

from smartdialer.config import CampaignSettings
from smartdialer.domain.stats import binom_tail_above, wilson_upper
from smartdialer.domain.types import PacingProposal, Snapshot

MAX_PROPOSAL = 1000


def propose(snap: Snapshot, cfg: CampaignSettings) -> PacingProposal:
    n_obs, ans = snap.window_n, snap.window_answered
    p = ans / n_obs if n_obs else 0.0
    p_hi = wilson_upper(ans, n_obs)
    soon_raw = cfg.soon_free_discount * snap.wrap_up
    soon = math.floor(soon_raw)
    capacity = snap.available + soon - snap.answered_waiting
    in_flight = snap.unbound_in_flight

    n = 0
    if capacity > 0:
        while n < MAX_PROPOSAL and binom_tail_above(capacity, in_flight + n + 1, p_hi) <= cfg.target_overflow:
            n += 1
    p_at = binom_tail_above(capacity, in_flight + n, p_hi)
    p_next = binom_tail_above(capacity, in_flight + n + 1, p_hi)

    explanation = (
        f"available={snap.available}, soon_free={cfg.soon_free_discount}x{snap.wrap_up}={soon_raw:.1f}->{soon}, "
        f"waiting={snap.answered_waiting}, ringing={in_flight}, p={p:.2f} (n={n_obs}), p_hi={p_hi:.2f}, "
        f"C={capacity}, target={cfg.target_overflow:.1%}. "
        f"n={n} gives P(overflow)={p_at:.2%}, n={n + 1} gives {p_next:.2%}. Proposing {n}."
        + (f" (avg setup {snap.avg_setup_s:.1f}s, avg talk {snap.avg_talk_s:.0f}s)" if snap.avg_setup_s and snap.avg_talk_s else "")
    )
    return PacingProposal(n, "PREDICTIVE", explanation, snap.taken_at)
