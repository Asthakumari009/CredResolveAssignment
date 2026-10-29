"""Demo: the provider sends ANSWERED, the worker processing it crashes, then COMPLETED arrives.

Variant 1: SIGKILL inside the transaction applying ANSWERED (before COMMIT): Postgres rolls it
           back, the inbox row stays unprocessed. Nobody bridged an agent while the borrower was
           on the line, and by the time another worker applies ANSWERED the call has ended
           (COMPLETED is waiting in the inbox). Expected: ABANDONED, counted toward the abandon
           budget, agent AVAILABLE, borrower DONE and not redialed. Same outcome as out_of_order.
Variant 2: SIGKILL right after COMMIT: ANSWERED (and the agent bridge) is durable; another
           worker just carries on with COMPLETED. Expected: COMPLETED, CONNECTED, not an abandon.
"""
import time

from demos.common import control, fresh_db, finish, header, print_call, say, services, start_worker, stop, wait_for
from demos.setup import campaign
from smartdialer.invariants import report


def run(conn, crash_at, title, expect, expect_abandons):
    header(title)
    control("A", auto_events=True, answer_rate=1.0, error_rate=0.0, busy_rate=0.0, talk_mean_s=100, time_scale=0.03)
    campaign(conn, agents=1, borrowers=1)
    w1 = start_worker("w1", crash_at=crash_at)
    w1.wait(timeout=30)
    say(f"w1 died with exit code {w1.returncode}")
    c = conn.execute("SELECT id, state FROM calls").fetchone()
    inbox = conn.execute("SELECT event_type, processing_result FROM provider_events ORDER BY id").fetchall()
    say(f"call state after the crash: {c['state']}; inbox: {[(r['event_type'], r['processing_result']) for r in inbox]}")
    say("waiting for the provider to deliver COMPLETED while no worker is running ...")
    wait_for(lambda: conn.execute("SELECT 1 FROM provider_events WHERE event_type = 'COMPLETED'").fetchone(), 20, "COMPLETED")
    time.sleep(0.3)
    w2 = start_worker("w2")
    ok = wait_for(lambda: conn.execute("SELECT state FROM borrowers").fetchone()["state"] == "DONE", 20, "borrower done")
    wait_for(lambda: conn.execute("SELECT state FROM agents").fetchone()["state"] == "AVAILABLE", 5, "agent free")
    stop(w2)
    print_call(conn, c["id"])
    final = conn.execute("SELECT c.state AS call, a.state AS agent, b.state AS borrower, b.last_outcome"
                         " FROM calls c, agents a, borrowers b").fetchone()
    print(f"\n  final: {final}")
    abandons = conn.execute("SELECT count(*) AS n FROM calls WHERE answered_at IS NOT NULL AND connected_at IS NULL"
                            " AND state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED')").fetchone()["n"]
    print(f"  counted toward the abandon budget: {abandons}")
    return ok and final == expect and abandons == expect_abandons and report(conn)


def main():
    with services():
        # Variant 1: the ANSWERED transaction rolled back, so no agent was bridged while the
        # borrower was on the line. By the time another worker applies it, the hold time is long
        # gone and COMPLETED is already in the inbox: that borrower was abandoned, and it counts.
        ok1 = run(fresh_db(), "event_answered_before_commit",
                  "CRASH AFTER ANSWERED, variant 1: worker SIGKILLed while applying ANSWERED (before COMMIT)",
                  {"call": "ABANDONED", "agent": "AVAILABLE", "borrower": "DONE", "last_outcome": "ABANDONED"}, 1)
        import httpx
        httpx.post("http://127.0.0.1:8001/control/reset")
        conn = fresh_db()
        # Variant 2: ANSWERED and the agent bridge committed before the crash: a real conversation.
        ok2 = run(conn, "event_answered_after_commit",
                  "CRASH AFTER ANSWERED, variant 2: worker SIGKILLed right after committing ANSWERED",
                  {"call": "COMPLETED", "agent": "AVAILABLE", "borrower": "DONE", "last_outcome": "CONNECTED"}, 0)
        finish(conn, ok1 and ok2)


if __name__ == "__main__":
    main()
