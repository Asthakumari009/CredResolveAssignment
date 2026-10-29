"""Demo: Provider B starts timing out on everything for 25 seconds, then recovers.

What to look for in the timeline:
  existing calls  keep running and their webhooks keep arriving, so they are tracked normally; any
                  whose events are lost are reconciled by get_call_status once the API is back
  new calls       dials time out -> uncertain; the breaker sees the failure rate and OPENs; the
                  Safety Controller then REJECTs every tick with PROVIDER_HEALTH: zero new dials
  retries         uncertain dials that the provider turns out never to have placed become
                  CANCELLED(NOT_DIALED) after recovery; given-up ones FAILED(TIMEOUT) and the
                  borrower is rescheduled with exponential backoff + jitter
  recovery        OPEN -> HALF_OPEN after 10 s -> 3 probe calls -> CLOSED -> pacing resumes
"""
import threading
import time

from demos.common import control, fresh_db, finish, header, say, services, start_worker, stop, wait_for
from demos.setup import campaign

SETTINGS = {"reconcile_pre_dial_s": 4, "reconcile_in_flight_s": 6, "reconcile_give_up_s": 15,
            "retry_base_s": 3, "max_attempts": 5}


def sample(conn, t0):
    r = conn.execute(
        """SELECT (SELECT breaker_state FROM provider_health WHERE provider_name = 'B') AS breaker,
                  (SELECT outcome || ' ' || approved_count || ' ' || array_to_string(reason_codes, ',')
                     FROM safety_decisions ORDER BY id DESC LIMIT 1) AS decision,
                  count(*) FILTER (WHERE state IN ('QUEUED','RESERVED')) AS pre_dial,
                  count(*) FILTER (WHERE state IN ('INITIATED','RINGING','ANSWERED','CONNECTED')) AS live,
                  count(*) FILTER (WHERE created_at > now() - interval '2 seconds') AS new_2s,
                  count(*) FILTER (WHERE failure_reason = 'TIMEOUT') AS failed_timeout,
                  count(*) FILTER (WHERE failure_reason = 'NOT_DIALED') AS not_dialed
           FROM calls""").fetchone()
    retry = conn.execute("SELECT count(*) AS n FROM borrowers WHERE state = 'PENDING' AND attempts > 0 AND next_attempt_at > now()").fetchone()["n"]
    print(f"  {time.monotonic() - t0:5.1f}s  {r['breaker']:<9} {r['pre_dial']:>8} {r['live']:>5} {r['new_2s']:>7} "
          f"{r['failed_timeout']:>7} {r['not_dialed']:>10} {retry:>9}   {r['decision']}", flush=True)


def main():
    header("PROVIDER OUTAGE: Provider B times out on everything for 25 s, then recovers")
    with services():
        conn = fresh_db()
        control("B", answer_rate=0.35, talk_mean_s=40, time_scale=0.05)
        campaign(conn, mode="PREDICTIVE", provider="B", agents=40, borrowers=5000, settings=SETTINGS)
        workers = [start_worker(f"w{i}") for i in range(1, 4)]
        t0 = time.monotonic()
        stop_flag = threading.Event()

        def sampler():
            sconn = __import__("smartdialer.db", fromlist=["connect"]).connect()
            print(f"\n  {'t':>5}  {'breaker':<9} {'pre_dial':>8} {'live':>5} {'new/2s':>7} {'timeout':>7} {'not_dialed':>10} {'backing off':>9}   last decision")
            while not stop_flag.wait(2):
                sample(sconn, t0)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        time.sleep(20)
        say("OUTAGE ON: Provider B API now hangs every request (dial, status, hangup)")
        control("B", outage=True)
        time.sleep(25)
        say("OUTAGE OFF")
        control("B", outage=False)
        recovered = wait_for(lambda: conn.execute(
            "SELECT 1 FROM safety_decisions WHERE at > now() - interval '1 second' AND approved_count > 0"
            " AND outcome <> 'REJECT'").fetchone() and conn.execute(
            "SELECT breaker_state FROM provider_health WHERE provider_name = 'B'").fetchone()["breaker_state"] == "CLOSED", 40, "recovery")
        time.sleep(6)
        stop_flag.set()
        th.join()
        stop(*workers)

        opened = conn.execute("SELECT count(*) AS n FROM safety_decisions WHERE 'PROVIDER_HEALTH' = ANY(reason_codes) AND outcome = 'REJECT'").fetchone()["n"]
        dialed_while_open = conn.execute(
            """SELECT count(*) AS n FROM calls c JOIN dial_permits p ON p.id = c.permit_id
               JOIN safety_decisions d ON d.id = p.decision_id WHERE d.outcome = 'REJECT'""").fetchone()["n"]
        print(f"\n  decisions rejected for PROVIDER_HEALTH: {opened}")
        print(f"  calls dialed against a REJECT decision: {dialed_while_open} (must be 0)")
        print(f"  breaker recovered to CLOSED and approvals resumed: {bool(recovered)}")
        finish(conn, opened > 0 and dialed_while_open == 0 and bool(recovered))


if __name__ == "__main__":
    main()
