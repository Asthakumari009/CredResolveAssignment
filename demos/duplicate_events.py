"""Demo: the same provider event arrives many times. Exactly one state transition results."""
from demos.common import control, fresh_db, header, finish, post_event, print_call, say, services
from demos.setup import campaign, progressive_tick
from smartdialer.events.processor import process_batch


def main():
    header("DUPLICATE EVENTS: ANSWERED delivered 5 times (3 exact redeliveries + 2 new event ids)")
    conn = fresh_db()
    with services():
        control("A", auto_events=False, error_rate=0.0)   # we play the provider's events by hand
        cid = campaign(conn, agents=1, borrowers=1)
        [call] = progressive_tick(conn, cid)
        say(f"call {call['id']} dialed via Provider A, agent reserved")

        say("POST RINGING")
        post_event("A", call["id"], "RINGING")
        for i in range(5):
            eid = "provider-evt-777" if i < 3 else None
            r = post_event("A", call["id"], "ANSWERED", event_id=eid)
            say(f"POST ANSWERED (event_id={eid or 'fresh'}) -> webhook says {r}")
        n = process_batch(conn, "demo-worker")
        say(f"event processor applied {n} inbox rows")
        post_event("A", call["id"], "COMPLETED", duration_s=75)
        process_batch(conn, "demo-worker")

        print_call(conn, call["id"])
        answered = conn.execute("SELECT count(*) AS n FROM call_transitions WHERE call_id = %s AND to_state = 'ANSWERED'",
                                (call["id"],)).fetchone()["n"]
        print(f"\n  ANSWERED transitions recorded: {answered} (expected 1)")
        print("  Barrier 1: inbox UNIQUE (provider, event_id) dropped the 2 exact redeliveries at insert.")
        print("  Barrier 2: the state machine classified the 2 re-sent facts as no-ops (the call had moved on).")
        print("  Barrier 3: UNIQUE (call_id, to_state) on call_transitions would reject a third copy anyway.")
        finish(conn, answered == 1)


if __name__ == "__main__":
    main()
