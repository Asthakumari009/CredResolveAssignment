"""Demo: 100 agents available on a predictive campaign; 40 go offline within 3 seconds.

Measures how quickly the Safety Controller stops approving overdial, and counts abandons
around the drop. Two mechanisms react:
  HARD_RATIO_CAP  uses agents AVAILABLE right now, so it shrinks on the very next tick;
  AGENT_DROP      sees staffed agents fall > 20% vs the recent max and forces progressive
                  for a 30 s cooldown.
Worst-case reaction is one pacing tick (250 ms) plus whatever was already in flight; the live
permit is revoked and replaced on every tick, so old approvals do not linger.
"""
import time

from demos.common import control, fresh_db, finish, header, say, services, start_worker, stop, wait_for
from demos.setup import campaign
from smartdialer.agent_actions import agent_action

SETTINGS = {"max_answer_hold_ms": 2000}


def decisions_table(conn, since, until):
    rows = conn.execute(
        """SELECT EXTRACT(EPOCH FROM at - %s) AS t, (snapshot->>'available')::int AS avail, (snapshot->>'staffed')::int AS staffed,
                  (snapshot->>'unbound_in_flight')::int AS ringing, requested, approved_count, outcome, mode, reason_codes
           FROM safety_decisions WHERE at BETWEEN %s AND %s ORDER BY at""", (since, since, until)).fetchall()
    print(f"\n  {'t(s)':>6} {'staffed':>7} {'avail':>5} {'ringing':>7} {'asked':>5} {'ok':>4}  outcome / mode / reasons")
    last = None
    for r in rows:
        key = (r["staffed"], r["avail"], r["outcome"], tuple(r["reason_codes"]))
        if key == last:
            continue
        last = key
        print(f"  {r['t']:6.2f} {r['staffed']:7} {r['avail']:5} {r['ringing']:7} {r['requested']:5} {r['approved_count']:4}  "
              f"{r['outcome']} / {r['mode']} / {','.join(r['reason_codes']) or '-'}")


def abandons(conn, start, end):
    return conn.execute(
        """SELECT count(*) FILTER (WHERE connected_at IS NULL) AS abandoned, count(*) AS answered FROM calls
           WHERE (answered_at IS NOT NULL OR answered_inferred) AND COALESCE(answered_at, ended_at) BETWEEN %s AND %s
             AND (connected_at IS NOT NULL OR state IN ('COMPLETED','ABANDONED'))""", (start, end)).fetchone()


def main():
    header("AGENT DROP: 100 agents, 40 go offline within 3 seconds (predictive, Provider A)")
    with services():
        conn = fresh_db()
        control("A", answer_rate=0.3, error_rate=0.0, talk_mean_s=120, time_scale=0.05)
        cid = campaign(conn, mode="PREDICTIVE", agents=100, borrowers=20000, settings=SETTINGS)
        workers = [start_worker(f"w{i}") for i in range(1, 5)]
        say("warming up until the campaign is dialing predictively ...")
        wait_for(lambda: conn.execute("SELECT count(*) AS n FROM safety_decisions WHERE mode = 'PREDICTIVE' AND approved_count > 0"
                                      ).fetchone()["n"] > 20, 60, "predictive approvals")
        time.sleep(8)

        drop_start = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        say("dropping 40 agents over 3 seconds")
        victims = [r["id"] for r in conn.execute("SELECT id FROM agents ORDER BY id LIMIT 40")]
        threshold_at = first_left_at = None
        for i, aid in enumerate(victims, 1):
            agent_action(conn, aid, "OFFLINE", "agent disappeared")
            if i == 1:
                first_left_at = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
            if i == 21:   # staffed falls below 80% of 100 here
                threshold_at = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
            time.sleep(3 / 40)
        drop_end = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        time.sleep(12)
        stop(*workers)

        decisions_table(conn, drop_start - (drop_end - drop_start), drop_end + (drop_end - drop_start) * 2)
        first_drop_rule = conn.execute(
            "SELECT min(at) AS t FROM safety_decisions WHERE at >= %s AND 'AGENT_DROP' = ANY(reason_codes)", (drop_start,)).fetchone()["t"]
        first_cap = conn.execute(
            """SELECT min(at) AS t FROM safety_decisions WHERE at >= %s AND mode = 'PREDICTIVE'
               AND 'HARD_RATIO_CAP' = ANY(reason_codes)""", (drop_start,)).fetchone()["t"]
        first_protective = conn.execute(
            """SELECT at, outcome, reason_codes FROM safety_decisions WHERE at >= %s AND outcome <> 'APPROVE'
               ORDER BY at LIMIT 1""", (first_left_at,)).fetchone()
        before = abandons(conn, drop_start - (drop_end - drop_start) * 5, drop_start)
        during = abandons(conn, drop_start, drop_end + (drop_end - drop_start) * 3)
        print("\n  Reaction")
        if first_protective:
            print(f"    TOTAL: first agent left -> first {first_protective['outcome']} ({','.join(first_protective['reason_codes'])}):"
                  f" {(first_protective['at'] - first_left_at).total_seconds() * 1000:.0f} ms")
            print("    (the drop itself is spread over 3 s; the rule fires once staffed is down 20%, then within one tick)")
        if first_cap:
            print(f"    first HARD_RATIO_CAP reduction after drop start: {(first_cap - drop_start).total_seconds() * 1000:6.0f} ms")
        print(f"    staffed fell below 80% at:                       {(threshold_at - drop_start).total_seconds() * 1000:6.0f} ms after drop start")
        if first_drop_rule:
            print(f"    first AGENT_DROP decision (forced progressive):   {(first_drop_rule - drop_start).total_seconds() * 1000:6.0f} ms after drop start"
                  f" ({(first_drop_rule - threshold_at).total_seconds() * 1000:.0f} ms after the threshold)")
        print("\n  Abandons (answered but never connected)")
        print(f"    15 s before the drop: {before['abandoned']} of {before['answered']} answered")
        print(f"    drop + 9 s after:     {during['abandoned']} of {during['answered']} answered")
        print("    (calls already ringing when the agents left can still be answered with nobody free;")
        print("     those are the abandons the hold-then-abandon path counts)")
        finish(conn, first_drop_rule is not None and (first_drop_rule - threshold_at).total_seconds() < 1.0)


if __name__ == "__main__":
    main()
