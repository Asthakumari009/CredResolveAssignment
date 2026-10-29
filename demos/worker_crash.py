"""Demo: agent reserved > borrower reserved > call initiated > worker killed (SIGKILL).

Two crash windows, each with a real worker process killing itself at the exact point:
  W2  the provider accepted the dial, the worker died before recording INITIATED
  W1  the dispatch transaction committed, the worker died before calling the provider
A second worker recovers each one. Checks: nobody double-dialed, no agent stuck.
"""
from demos.common import (control, fresh_db, finish, header, print_call, provider_stats, say, services,
                          start_worker, stop, wait_for)
from demos.setup import campaign

SETTINGS = {"reconcile_pre_dial_s": 3}


def snapshot(conn, label):
    c = conn.execute("SELECT id, state, provider_call_id FROM calls ORDER BY created_at LIMIT 1").fetchone()
    a = conn.execute("SELECT state, reserved_call_id FROM agents").fetchone()
    b = conn.execute("SELECT phone, state, attempts FROM borrowers ORDER BY id LIMIT 1").fetchone()
    w = conn.execute("SELECT id, round(EXTRACT(EPOCH FROM now() - last_heartbeat_at)::numeric, 1) AS age FROM workers").fetchall()
    say(label)
    print(f"    call     {c['id']}  state={c['state']}  provider_call_id={c['provider_call_id']}")
    print(f"    agent    state={a['state']}  reserved_call_id={a['reserved_call_id']}")
    print(f"    borrower {b['phone']} state={b['state']} attempts={b['attempts']}")
    print(f"    workers  " + ", ".join(f"{r['id']} last heartbeat {r['age']}s ago" for r in w))
    return c["id"]


def run_window(conn, crash_at, title, expect):
    header(title)
    control("A", auto_events=True, answer_rate=1.0, error_rate=0.0, talk_mean_s=60, time_scale=0.03)
    cid = campaign(conn, agents=1, borrowers=2, settings=SETTINGS)
    w1 = start_worker("w1", crash_at=crash_at)
    w1.wait(timeout=30)
    say(f"w1 exit code {w1.returncode} (-9 = SIGKILL)")
    first = snapshot(conn, "state left behind by the dead worker:")
    st = __import__("httpx").get(f"http://127.0.0.1:8001/a/calls/{first}")
    say(f"provider's truth for idempotency key {str(first)[:8]}: {st.json() if st.status_code == 200 else 'NOT_FOUND (404)'}")

    w2 = start_worker("w2")
    done = wait_for(lambda: conn.execute("SELECT count(*) AS n FROM borrowers WHERE state = 'DONE'").fetchone()["n"] == 2,
                    40, "both borrowers done")
    stop(w2)
    print_call(conn, first)
    stats = provider_stats("A")
    print(f"\n  provider placed {stats['calls']} calls; per phone: {stats['by_phone']}")
    print(f"  expected: {expect}")
    ok = done and all(n == 1 for n in stats["by_phone"].values())
    return ok


def main():
    with services():
        conn = fresh_db()
        ok1 = run_window(conn, "after_dial_before_initiated",
                         "WORKER CRASH, window W2: provider accepted the dial, worker died before recording INITIATED",
                         "w2 adopts the live call (events and/or get_call_status), no redial: 1 provider call per phone")
        okw2 = finish_part(conn)
        __import__("httpx").post("http://127.0.0.1:8001/control/reset")
        conn = fresh_db()
        ok2 = run_window(conn, "after_commit_before_dial",
                         "WORKER CRASH, window W1: dispatch committed, worker died before calling the provider",
                         "provider says NOT_FOUND -> CANCELLED(NOT_DIALED), agent+borrower released, borrower dialed"
                         " later with a NEW call id: still 1 provider call per phone")
        finish(conn, ok1 and okw2 and ok2)


def finish_part(conn) -> bool:
    from smartdialer.invariants import report
    return report(conn)


if __name__ == "__main__":
    main()
