import itertools
import random

import pytest

from smartdialer.domain.call_sm import (ALLOWED, EVENT_TARGETS, RANK, TERMINAL, CallState as S,
                                        Verdict, classify)

# Hand-written copy of the table. If someone edits ALLOWED, this test makes them say so twice.
EXPECTED_ALLOWED = {
    ("QUEUED", "INITIATED"), ("QUEUED", "RINGING"), ("QUEUED", "ANSWERED"),
    ("RESERVED", "INITIATED"), ("RESERVED", "RINGING"), ("RESERVED", "ANSWERED"),
    ("INITIATED", "RINGING"), ("INITIATED", "ANSWERED"),
    ("RINGING", "ANSWERED"),
    ("ANSWERED", "CONNECTED"),
    ("QUEUED", "COMPLETED"), ("RESERVED", "COMPLETED"), ("INITIATED", "COMPLETED"),
    ("RINGING", "COMPLETED"), ("ANSWERED", "COMPLETED"), ("CONNECTED", "COMPLETED"),
    ("QUEUED", "FAILED"), ("RESERVED", "FAILED"), ("INITIATED", "FAILED"), ("RINGING", "FAILED"),
    ("QUEUED", "CANCELLED"), ("RESERVED", "CANCELLED"), ("INITIATED", "CANCELLED"), ("RINGING", "CANCELLED"),
    ("ANSWERED", "ABANDONED"),
}


def test_table_matches_expected():
    assert {(a.value, b.value) for a, b in ALLOWED} == EXPECTED_ALLOWED


@pytest.mark.parametrize("frm,to", list(itertools.product([s.value for s in S], repeat=2)))
def test_every_pair(frm, to):
    verdict = classify(frm, to)
    if frm == to:
        assert verdict == Verdict.DUPLICATE
    elif S(frm) in TERMINAL:
        assert verdict == Verdict.STALE_IGNORED, "terminal states are absorbing"
    elif RANK[S(to)] < RANK[S(frm)]:
        assert verdict == Verdict.STALE_IGNORED, "ranks never go backwards"
    elif (frm, to) in EXPECTED_ALLOWED:
        assert verdict == Verdict.APPLY
    else:
        assert verdict == Verdict.REJECTED


def fold(state, events):
    """Apply provider events with the same rules the event processor uses."""
    applied = []
    for ev in events:
        target, _ = EVENT_TARGETS[ev]
        v = classify(state, target)
        if v == Verdict.APPLY:
            state = target.value
            applied.append(state)
    return state, applied


def test_assignment_sequence_triple_answered():
    state, applied = fold("INITIATED", ["ANSWERED", "ANSWERED", "ANSWERED", "COMPLETED"])
    assert state == "COMPLETED"
    assert applied == ["ANSWERED", "COMPLETED"]


def test_assignment_sequence_reversed():
    state, applied = fold("INITIATED", ["COMPLETED", "ANSWERED", "RINGING"])
    assert state == "COMPLETED"
    assert applied == ["COMPLETED"]
    assert classify("COMPLETED", "ANSWERED") == Verdict.STALE_IGNORED
    assert classify("COMPLETED", "RINGING") == Verdict.STALE_IGNORED


def test_no_answer_after_answered_is_rejected():
    assert classify("ANSWERED", "FAILED") == Verdict.REJECTED


LIFECYCLES = [
    ["RINGING", "ANSWERED", "COMPLETED"],
    ["RINGING", "NO_ANSWER"],
    ["RINGING", "BUSY"],
    ["FAILED"],
]


@pytest.mark.parametrize("seed", range(300))
def test_property_duplicated_shuffled_dropped(seed):
    """Random duplication, shuffling and dropping of intermediate events never produces an
    invalid or repeated transition, and always ends terminal when the final event survives."""
    rng = random.Random(seed)
    life = rng.choice(LIFECYCLES)
    events = [e for e in life[:-1] if rng.random() > 0.3] + [life[-1]]   # drop intermediates
    events = [e for e in events for _ in range(rng.randint(1, 3))]        # duplicate
    rng.shuffle(events)                                                   # reorder
    start = rng.choice(["QUEUED", "RESERVED", "INITIATED"])
    state, applied = fold(start, events)
    assert S(state) in TERMINAL
    assert len(applied) == len(set(applied)), "a state was entered twice"
    prev = start
    for nxt in applied:
        assert (S(prev), S(nxt)) in ALLOWED
        prev = nxt
