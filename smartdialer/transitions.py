"""The only code that changes call, agent and borrower state once a call row exists.

Dial responses, provider events, reconciliation and the answer-hold loop all go through
`advance_call()`. It must be called inside a transaction. It:

  1. locks the call row (SELECT ... FOR UPDATE), which serializes everything that happens
     to one call. A pessimistic lock rather than an optimistic version check, because events
     for one call must be applied strictly one at a time anyway; optimistic checks would only
     turn the same serialization into retries. The lock is held for milliseconds and never
     across network I/O.
  2. asks the pure state machine (call_sm.classify) whether the move is allowed;
  3. writes the history row with ON CONFLICT DO NOTHING (UNIQUE (call_id, to_state) is the
     backstop; a conflict is reported as DUPLICATE and the transaction stays usable);
  4. applies side effects to the borrower and then the agent.

LOCK ORDER: call -> borrower -> agent (see db.py).
"""
import random

from smartdialer.config import load_settings
from smartdialer.domain.agent_sm import can_transition
from smartdialer.domain.backoff import retry_delay_s
from smartdialer.domain.call_sm import RANK, TERMINAL, CallState as S, Verdict, classify

TIMESTAMP_COLUMN = {S.INITIATED: "dialed_at", S.ANSWERED: "answered_at", S.CONNECTED: "connected_at"}


# ----------------------------------------------------------------------------- agents

def log_agent(conn, agent_id, frm, to, call_id, worker_id, reason):
    conn.execute(
        "INSERT INTO agent_transitions (agent_id, from_state, to_state, call_id, worker_id, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s)", (agent_id, frm, to, call_id, worker_id, reason))


def set_agent_state(conn, agent_id, to, *, fence_call_id, worker_id, reason) -> bool:
    """Worker-driven agent transition after reservation. Returns False if the fence does not match.

    # CONCURRENCY: a stale worker (crashed and restarted, slow, or processing an old call) may
    # hold a call id the agent is no longer reserved for. If the agent has been released and
    # re-reserved since, reserved_call_id differs and the SELECT finds nothing: the stale worker
    # changes nothing. Call ids are never reused, so this cannot be fooled by the agent passing
    # through the same states again (no ABA problem).
    """
    agent = conn.execute(
        "SELECT state FROM agents WHERE id = %s AND reserved_call_id = %s FOR UPDATE",
        (agent_id, fence_call_id)).fetchone()
    if agent is None or agent["state"] == to or not can_transition(agent["state"], to):
        return False
    release = to in ("AVAILABLE", "PAUSED", "OFFLINE")
    conn.execute(
        """UPDATE agents SET state = %(to)s, state_changed_at = clock_timestamp(),
               reserved_call_id   = CASE WHEN %(release)s THEN NULL ELSE reserved_call_id END,
               reserved_by_worker = CASE WHEN %(release)s THEN NULL ELSE reserved_by_worker END
           WHERE id = %(id)s AND reserved_call_id = %(fence)s AND state = %(frm)s""",
        {"to": to, "release": release, "id": agent_id, "fence": fence_call_id, "frm": agent["state"]})
    log_agent(conn, agent_id, agent["state"], to, fence_call_id, worker_id, reason)
    return True


def reserve_agent(conn, campaign_id, call_id, worker_id) -> int | None:
    """Progressive: AVAILABLE -> RESERVED for this call. Returns the agent id, or None.

    # CONCURRENCY: two workers can both see this agent as AVAILABLE. FOR UPDATE SKIP LOCKED makes
    # the second worker skip a row the first has locked and take the next agent instead of
    # waiting. Even without SKIP LOCKED only one UPDATE could win: Postgres re-checks
    # state = 'AVAILABLE' on the locked row after the first commits, and the loser matches 0 rows.
    """
    row = conn.execute(
        """UPDATE agents SET state = 'RESERVED', reserved_call_id = %(call)s,
               reserved_by_worker = %(w)s, state_changed_at = clock_timestamp()
           WHERE id = (SELECT id FROM agents WHERE campaign_id = %(c)s AND state = 'AVAILABLE'
                       ORDER BY state_changed_at LIMIT 1 FOR UPDATE SKIP LOCKED)
             AND state = 'AVAILABLE'
           RETURNING id""", {"call": call_id, "w": worker_id, "c": campaign_id}).fetchone()
    if row is None:
        return None
    log_agent(conn, row["id"], "AVAILABLE", "RESERVED", call_id, worker_id, "progressive reserve")
    return row["id"]


def claim_agent(conn, campaign_id, call_id, worker_id) -> int | None:
    """An answered call needs an agent now: AVAILABLE -> CONNECTED. Same atomicity as reserve."""
    row = conn.execute(
        """UPDATE agents SET state = 'CONNECTED', reserved_call_id = %(call)s,
               reserved_by_worker = %(w)s, state_changed_at = clock_timestamp()
           WHERE id = (SELECT id FROM agents WHERE campaign_id = %(c)s AND state = 'AVAILABLE'
                       ORDER BY state_changed_at LIMIT 1 FOR UPDATE SKIP LOCKED)
             AND state = 'AVAILABLE'
           RETURNING id""", {"call": call_id, "w": worker_id, "c": campaign_id}).fetchone()
    if row is None:
        return None
    log_agent(conn, row["id"], "AVAILABLE", "CONNECTED", call_id, worker_id, "claimed for answered call")
    return row["id"]


# ----------------------------------------------------------------------------- calls

def record_creation(conn, call_id, state, worker_id):
    conn.execute("INSERT INTO call_transitions (call_id, from_state, to_state, worker_id) VALUES (%s, NULL, %s, %s)",
                 (call_id, state, worker_id))


def advance_call(conn, call_id, target, *, worker_id, reason=None, cause_event_id=None,
                 provider_call_id=None, duration_s=None, rng=None):
    """Move a call towards `target`. Returns (verdict, call_row_after). Must run inside a transaction."""
    target = S(target)
    call = conn.execute("SELECT * FROM calls WHERE id = %s FOR UPDATE", (call_id,)).fetchone()
    if call is None:
        raise LookupError(call_id)
    if provider_call_id and not call["provider_call_id"]:
        # Recorded whatever the verdict: a late dial response still tells us the provider's id.
        conn.execute("UPDATE calls SET provider_call_id = %s WHERE id = %s", (provider_call_id, call_id))
        call["provider_call_id"] = provider_call_id

    verdict = classify(call["state"], target)
    if verdict != Verdict.APPLY:
        return verdict, call

    inserted = conn.execute(
        "INSERT INTO call_transitions (call_id, from_state, to_state, cause_event_id, worker_id)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (call_id, to_state) DO NOTHING RETURNING id",
        (call_id, call["state"], target.value, cause_event_id, worker_id)).fetchone()
    if inserted is None:
        return Verdict.DUPLICATE, call

    updates = {"state": target.value, "state_rank": RANK[target]}
    if target in TIMESTAMP_COLUMN:
        updates[TIMESTAMP_COLUMN[target]] = "now"
    if target in TERMINAL:
        updates["ended_at"] = "now"
        updates["failure_reason"] = reason
        if duration_s is not None:
            updates["duration_s"] = duration_s
            if call["answered_at"] is None and duration_s > 0:
                updates["answered_inferred"] = True   # COMPLETED without ANSWERED: infer for stats
    sets = ", ".join(f"{k} = clock_timestamp()" if v == "now" else f"{k} = %({k})s" for k, v in updates.items())
    call = conn.execute(f"UPDATE calls SET {sets}, state_changed_at = clock_timestamp() WHERE id = %(id)s RETURNING *",
                        {**updates, "id": call_id}).fetchone()

    if target in TERMINAL:
        _settle_borrower(conn, call, rng)
    if call["agent_id"] is not None:
        _sync_agent(conn, call, worker_id)
    if target == S.ANSWERED:
        call = try_connect(conn, call, worker_id)
    return Verdict.APPLY, call


def try_connect(conn, call, worker_id):
    """Answered call: bridge an agent. Progressive uses its reserved agent; if that agent is gone,
    or the call is predictive, claim any AVAILABLE agent. If none, the call stays ANSWERED and
    the answer-hold loop either connects it later or abandons it after max_answer_hold_ms."""
    if call["state"] != S.ANSWERED:
        return call
    agent_ok = call["agent_id"] is not None and conn.execute(
        "SELECT 1 FROM agents WHERE id = %s AND reserved_call_id = %s AND state = 'CONNECTED'",
        (call["agent_id"], call["id"])).fetchone() is not None
    if not agent_ok:
        new_agent = claim_agent(conn, call["campaign_id"], call["id"], worker_id)
        if new_agent is None:
            return call
        conn.execute("UPDATE calls SET agent_id = %s WHERE id = %s", (new_agent, call["id"]))
    _, call = advance_call(conn, call["id"], S.CONNECTED, worker_id=worker_id)
    return call


def _sync_agent(conn, call, worker_id):
    """Bring the call's agent to the state that matches the call. Fenced by reserved_call_id."""
    state = S(call["state"])
    if state in (S.INITIATED, S.RINGING):
        desired = ["DIALING"]
    elif state in (S.ANSWERED, S.CONNECTED):
        desired = ["CONNECTED"]
    elif state == S.COMPLETED:
        desired = ["WRAP_UP", "AVAILABLE"]   # WRAP_UP if they talked, else straight back
    elif state in TERMINAL:
        desired = ["AVAILABLE"]
    else:
        return
    for to in desired:
        if set_agent_state(conn, call["agent_id"], to, fence_call_id=call["id"], worker_id=worker_id,
                           reason=f"call {state.value}"):
            return


def _settle_borrower(conn, call, rng):
    """Terminal call: decide the borrower's next step. Never retry a call that was answered."""
    cfg = load_settings(conn, call["campaign_id"])
    answered = call["answered_at"] is not None or call["answered_inferred"]
    state, reason = call["state"], call["failure_reason"]
    if state == S.ABANDONED or (answered and call["connected_at"] is None):
        new, outcome, delay, undo_attempt = "DONE", "ABANDONED", 0.0, False
    elif answered:
        new, outcome, delay, undo_attempt = "DONE", "CONNECTED", 0.0, False
    elif state == S.CANCELLED and call["dialed_at"] is None and call["provider_call_id"] is None:
        # Never confirmed dialed (provider had no such call, or the agent left before the dial
        # was recorded): does not count as an attempt, try again now.
        new, outcome, delay, undo_attempt = "PENDING", "NOT_DIALED", 0.0, True
    else:
        b = conn.execute("SELECT attempts FROM borrowers WHERE id = %s", (call["borrower_id"],)).fetchone()
        exhausted = b["attempts"] >= cfg.max_attempts
        new = "EXHAUSTED" if exhausted else "PENDING"
        outcome = reason or state
        delay = 0.0 if exhausted else retry_delay_s(b["attempts"], cfg.retry_base_s, cfg.retry_cap_s, rng)
        undo_attempt = False
    conn.execute(
        """UPDATE borrowers SET state = %(new)s, last_outcome = %(outcome)s, locked_by_worker = NULL,
               next_attempt_at = now() + %(delay)s * interval '1 second',
               attempts = attempts - %(undo)s, updated_at = clock_timestamp()
           WHERE id = %(id)s AND state = 'IN_PROGRESS'""",
        {"new": new, "outcome": outcome, "delay": delay, "undo": int(undo_attempt), "id": call["borrower_id"]})
