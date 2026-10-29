"""Event processor: any worker drains the provider_events inbox.

One event per transaction:
    lock one unprocessed inbox row (SKIP LOCKED)  ->  advance_call (locks the call row)
    ->  mark the inbox row processed with its result
all committed together. If the worker dies mid-way, Postgres rolls the whole thing back, the
inbox row is unlocked and still unprocessed, and another worker applies it. There is no
in-memory queue to lose.

LOCK ORDER: inbox row -> call -> borrower -> agent.
"""
from smartdialer.domain.call_sm import EVENT_TARGETS, SYNTHETIC_ONLY, Verdict
from smartdialer.faults import crash_point
from smartdialer.transitions import advance_call

ORPHAN_AFTER_ATTEMPTS = 5
RESEQUENCE_WINDOW_MS = 1500
RESULT = {Verdict.APPLY: "APPLIED", Verdict.DUPLICATE: "DUPLICATE",
          Verdict.STALE_IGNORED: "STALE_IGNORED", Verdict.REJECTED: "REJECTED"}


def process_one(conn, worker_id: str) -> dict | None:
    """Apply the next due inbox event. Returns the processed event row, or None if the inbox is empty."""
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
            verdict, _ = advance_call(conn, call_id, target, worker_id=worker_id, reason=reason,
                                      cause_event_id=ev["id"], provider_call_id=ev["provider_call_id"],
                                      duration_s=(ev["payload"] or {}).get("duration_s"))
            result = RESULT[verdict]
        crash_point(f"event_{ev['event_type'].lower()}_before_commit")
        conn.execute("UPDATE provider_events SET processed_at = now(), processing_result = %s, call_id = %s WHERE id = %s",
                     (result, call_id, ev["id"]))
    crash_point(f"event_{ev['event_type'].lower()}_after_commit")
    return {**ev, "call_id": call_id, "processing_result": result}


def process_batch(conn, worker_id: str, limit: int = 100) -> int:
    n = 0
    while n < limit and process_one(conn, worker_id) is not None:
        n += 1
    return n
