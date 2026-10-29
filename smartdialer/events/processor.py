"""Event processor: any worker drains the provider_events inbox.

One event per transaction:
    lock one unprocessed inbox row (SKIP LOCKED)  ->  advance_call (locks the call row)
    ->  mark the inbox row processed with its result
all committed together. If the worker dies mid-way, Postgres rolls the whole thing back, the
inbox row is unlocked and still unprocessed, and another worker applies it. There is no
in-memory queue to lose.

LOCK ORDER: inbox row -> call -> borrower -> agent.

A late ANSWERED is an abandon, not a connect. If we apply ANSWERED more than max_answer_hold_ms
after the borrower picked up (a worker died holding it, or the inbox is backed up), or the
provider has already told us the call ended, nobody was bridged to that borrower in time. The
call is recorded ANSWERED (unbridged) then ABANDONED, so it counts toward the abandon budget, the
same as an answer that arrives after COMPLETED (out_of_order demo). If the call may still be up,
it is hung up after commit.
"""
from smartdialer.config import load_settings
from smartdialer.domain.call_sm import EVENT_TARGETS, SYNTHETIC_ONLY, Verdict
from smartdialer.faults import crash_point
from smartdialer.transitions import advance_call

ORPHAN_AFTER_ATTEMPTS = 5
RESEQUENCE_WINDOW_MS = 1500
RESULT = {Verdict.APPLY: "APPLIED", Verdict.DUPLICATE: "DUPLICATE",
          Verdict.STALE_IGNORED: "STALE_IGNORED", Verdict.REJECTED: "REJECTED"}


# The only terminal event the state machine accepts after ANSWERED. A NO_ANSWER / BUSY / FAILED
# after an answer is contradictory provider data and is REJECTED, so it is not evidence either.
_ENDED_AFTER_ANSWER = ('COMPLETED',)


def _answer_too_late(conn, ev, call_id) -> tuple[bool, bool]:
    """(too_late, call_already_ended). The answer time is the earliest evidence we have: the
    provider's timestamp or our receive time, whichever is earlier (errs toward an abandon)."""
    hold_ms = load_settings(conn, conn.execute("SELECT campaign_id FROM calls WHERE id = %s",
                                               (call_id,)).fetchone()["campaign_id"]).max_answer_hold_ms
    r = conn.execute(
        """SELECT now() - LEAST(COALESCE(%(pt)s, %(rt)s), %(rt)s) > %(hold)s * interval '1 millisecond' AS stale,
                  EXISTS (SELECT 1 FROM provider_events o
                          WHERE o.call_id = %(c)s AND o.processed_at IS NULL AND o.id <> %(id)s
                            AND o.event_type = ANY(%(term)s)) AS ended""",
        {"pt": ev["provider_timestamp"], "rt": ev["received_at"], "hold": hold_ms, "c": call_id,
         "id": ev["id"], "term": list(_ENDED_AFTER_ANSWER)}).fetchone()
    return r["stale"] or r["ended"], r["ended"]


def process_one(conn, worker_id: str, providers: dict | None = None) -> dict | None:
    """Apply the next due inbox event. Returns the processed event row, or None if the inbox is empty."""
    hangup = None
    with conn.transaction():
        # CONCURRENCY: SKIP LOCKED hands each inbox row to exactly one worker. The NOT EXISTS keeps
        # events of ONE call in order: an event is only eligible if no earlier unprocessed event
        # (by provider sequence, then arrival) exists for the same call. Without it, two workers
        # could take ANSWERED and COMPLETED of one call at once and, racing for the call row lock,
        # apply COMPLETED first: our own concurrency would reorder a perfectly ordered provider.
        ev = conn.execute(
            """SELECT * FROM provider_events e
               WHERE e.processed_at IS NULL AND e.next_attempt_at <= now()
                 AND NOT EXISTS (
                     SELECT 1 FROM provider_events o
                     WHERE o.call_id = e.call_id AND o.processed_at IS NULL
                       AND (COALESCE(o.sequence, 2147483647), o.id) < (COALESCE(e.sequence, 2147483647), e.id))
               ORDER BY e.next_attempt_at, e.id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
        if ev is None:
            return None
        call_id = ev["call_id"]
        if call_id is None and ev["provider_call_id"]:
            r = conn.execute("SELECT id FROM calls WHERE provider_name = %s AND provider_call_id = %s",
                             (ev["provider_name"], ev["provider_call_id"])).fetchone()
            call_id = r and r["id"]
        known = call_id is not None and conn.execute("SELECT 1 FROM calls WHERE id = %s", (call_id,)).fetchone()
        if not known:
            # Orphan: keep it, retry briefly (in case it raced ahead of something), then give up.
            final = ev["attempts"] + 1 >= ORPHAN_AFTER_ATTEMPTS
            conn.execute(
                """UPDATE provider_events SET attempts = attempts + 1, next_attempt_at = now() + interval '1 second',
                       processed_at = CASE WHEN %(f)s THEN now() END, processing_result = CASE WHEN %(f)s THEN 'ORPHAN' END
                   WHERE id = %(id)s""", {"f": final, "id": ev["id"]})
            return {**ev, "processing_result": "ORPHAN" if final else None}

        # Resequencing hold: if the provider numbers its events and this one skips ahead (e.g.
        # COMPLETED seq 3 while we have applied only seq 1), give the missing event a short window
        # to arrive. Safety never depends on this (the state machine already ignores late events);
        # it only avoids losing the information an ANSWERED that arrives 300 ms late carries.
        call = conn.execute("SELECT last_event_seq FROM calls WHERE id = %s FOR UPDATE", (call_id,)).fetchone()
        seq = ev["sequence"]
        if seq is not None and seq > call["last_event_seq"] + 1 and conn.execute(
                "SELECT %s > now() - %s * interval '1 millisecond' AS young", (ev["received_at"], RESEQUENCE_WINDOW_MS)).fetchone()["young"]:
            conn.execute("UPDATE provider_events SET next_attempt_at = now() + interval '200 milliseconds' WHERE id = %s", (ev["id"],))
            return {**ev, "processing_result": None}
        if seq is not None:
            conn.execute("UPDATE calls SET last_event_seq = GREATEST(last_event_seq, %s) WHERE id = %s", (seq, call_id))

        forged = ev["event_type"] in SYNTHETIC_ONLY and not ev["provider_event_id"].startswith("reconcile:")
        if ev["event_type"] not in EVENT_TARGETS or forged:   # a provider cannot send NOT_FOUND / TIMEOUT
            result = "REJECTED"
        else:
            target, reason = EVENT_TARGETS[ev["event_type"]]
            late, ended = _answer_too_late(conn, ev, call_id) if ev["event_type"] == "ANSWERED" else (False, False)
            verdict, after = advance_call(conn, call_id, target, worker_id=worker_id, reason=reason,
                                          cause_event_id=ev["id"], provider_call_id=ev["provider_call_id"],
                                          duration_s=(ev["payload"] or {}).get("duration_s"), bridge=not late)
            if late and verdict == Verdict.APPLY:
                _, after = advance_call(conn, call_id, "ABANDONED", worker_id=worker_id, reason="ANSWER_PROCESSED_TOO_LATE")
                if not ended and after["provider_call_id"]:
                    hangup = after
            result = RESULT[verdict]
        crash_point(f"event_{ev['event_type'].lower()}_before_commit")
        conn.execute("UPDATE provider_events SET processed_at = now(), processing_result = %s, call_id = %s WHERE id = %s",
                     (result, call_id, ev["id"]))
    crash_point(f"event_{ev['event_type'].lower()}_after_commit")
    if hangup and providers:   # network I/O only after commit
        providers[hangup["provider_name"]].hangup(hangup["provider_call_id"], "abandon: answer processed too late")
    return {**ev, "call_id": call_id, "processing_result": result}


def process_batch(conn, worker_id: str, limit: int = 100, providers: dict | None = None) -> int:
    n = 0
    while n < limit and process_one(conn, worker_id, providers) is not None:
        n += 1
    return n
