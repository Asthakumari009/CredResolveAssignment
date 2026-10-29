"""Call state machine: explicit allowed-transitions table plus ranks.

Every change to a call's state, whether caused by a provider event, a dial response, the
reaper or the answer-hold loop, goes through `classify()`. Its verdict decides what happens:

    APPLY          the transition is in the table: apply it
    DUPLICATE      target == current state (e.g. ANSWERED twice): no-op
    STALE_IGNORED  call already terminal, or target ranks below current (late event): no-op
    REJECTED       moves forward but is not in the table (e.g. NO_ANSWER after ANSWERED): no-op, logged

Ranks only ever increase, so a call enters each state at most once. That is what makes
UNIQUE (call_id, to_state) on call_transitions a valid backstop.
"""
from enum import StrEnum


class CallState(StrEnum):
    QUEUED = "QUEUED"          # predictive call created, no agent yet
    RESERVED = "RESERVED"      # progressive call created with its agent reserved
    INITIATED = "INITIATED"    # provider accepted the dial
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"      # borrower picked up; no agent bridged yet
    CONNECTED = "CONNECTED"    # borrower talking to an agent
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"          # failure_reason: NO_ANSWER, BUSY, TIMEOUT, PROVIDER_ERROR
    CANCELLED = "CANCELLED"    # failure_reason: NOT_DIALED, AGENT_LOST
    ABANDONED = "ABANDONED"    # answered, no agent within max_answer_hold_ms


class Verdict(StrEnum):
    APPLY = "APPLY"
    DUPLICATE = "DUPLICATE"
    STALE_IGNORED = "STALE_IGNORED"
    REJECTED = "REJECTED"


S = CallState

TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED, S.ABANDONED})
PRE_DIAL = frozenset({S.QUEUED, S.RESERVED})

RANK: dict[CallState, int] = {
    S.QUEUED: 0, S.RESERVED: 1, S.INITIATED: 2, S.RINGING: 3, S.ANSWERED: 4, S.CONNECTED: 5,
    S.COMPLETED: 9, S.FAILED: 9, S.CANCELLED: 9, S.ABANDONED: 9,
}

_NOT_ANSWERED = (S.QUEUED, S.RESERVED, S.INITIATED, S.RINGING)

ALLOWED: frozenset[tuple[CallState, CallState]] = frozenset(
    # Forward moves may skip states: Provider B drops intermediate events, and events can
    # arrive before our own dial response has been recorded.
    {(S.QUEUED, t) for t in (S.INITIATED, S.RINGING, S.ANSWERED)}
    | {(S.RESERVED, t) for t in (S.INITIATED, S.RINGING, S.ANSWERED)}
    | {(S.INITIATED, S.RINGING), (S.INITIATED, S.ANSWERED), (S.RINGING, S.ANSWERED)}
    | {(S.ANSWERED, S.CONNECTED)}
    # COMPLETED is accepted from any live state ("COMPLETED without ANSWERED" is allowed;
    # stats infer the answer from duration > 0).
    | {(f, S.COMPLETED) for f in (*_NOT_ANSWERED, S.ANSWERED, S.CONNECTED)}
    # Failure and cancellation only make sense before anyone answered.
    | {(f, S.FAILED) for f in _NOT_ANSWERED}
    | {(f, S.CANCELLED) for f in _NOT_ANSWERED}
    # Abandon is only possible for an answered call that never got an agent.
    | {(S.ANSWERED, S.ABANDONED)}
)

# Provider event type -> (target state, failure_reason)
EVENT_TARGETS: dict[str, tuple[CallState, str | None]] = {
    "INITIATED": (S.INITIATED, None),
    "RINGING": (S.RINGING, None),
    "ANSWERED": (S.ANSWERED, None),
    "COMPLETED": (S.COMPLETED, None),
    "NO_ANSWER": (S.FAILED, "NO_ANSWER"),
    "BUSY": (S.FAILED, "BUSY"),
    "FAILED": (S.FAILED, "PROVIDER_ERROR"),
    "NOT_FOUND": (S.CANCELLED, "NOT_DIALED"),   # synthesized by reconciliation, never by a provider
}


def classify(current: str, target: str) -> Verdict:
    cur, tgt = CallState(current), CallState(target)
    if cur == tgt:
        return Verdict.DUPLICATE
    if cur in TERMINAL or RANK[tgt] < RANK[cur]:
        return Verdict.STALE_IGNORED
    if (cur, tgt) in ALLOWED:
        return Verdict.APPLY
    return Verdict.REJECTED


def is_terminal(state: str) -> bool:
    return CallState(state) in TERMINAL
