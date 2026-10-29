"""Demo: events arrive as COMPLETED, ANSWERED, RINGING. The call ends COMPLETED; late events are ignored."""
from demos.common import control, fresh_db, header, finish, post_event, print_call, say, services
from demos.setup import campaign, progressive_tick
from smartdialer.events.processor import process_batch


def main():
    header("OUT-OF-ORDER EVENTS: COMPLETED, then ANSWERED, then RINGING")
    conn = fresh_db()
    with services():
        control("A", auto_events=False, error_rate=0.0)
        cid = campaign(conn, agents=1, borrowers=1)
        [call] = progressive_tick(conn, cid)
        for etype, dur in (("COMPLETED", 42.0), ("ANSWERED", None), ("RINGING", None)):
            say(f"POST {etype}")
            post_event("A", call["id"], etype, duration_s=dur)
        process_batch(conn, "demo-worker")
        print_call(conn, call["id"])
        c = conn.execute("SELECT state, answered_inferred, duration_s FROM calls WHERE id = %s", (call["id"],)).fetchone()
        a = conn.execute("SELECT state FROM agents").fetchone()
        b = conn.execute("SELECT state, last_outcome FROM borrowers").fetchone()
        print(f"\n  final call: {c}")
        print(f"  agent: {a['state']} (never bridged, so straight back to AVAILABLE, no wrap-up)")
        print(f"  borrower: {b} (answer inferred from duration > 0 but no agent was ever connected:")
        print("            counted as an abandon, conservatively, and never redialed)")
        ok = c["state"] == "COMPLETED" and c["answered_inferred"] and a["state"] == "AVAILABLE"
        finish(conn, ok)


if __name__ == "__main__":
    main()
