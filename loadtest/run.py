"""Load test: 1,000 agents, 50,000 borrowers, N worker processes, Provider A, short talk times.

    python -m loadtest.run                     # 8 workers, 60 s
    python -m loadtest.run --workers 8 16 32   # the 1x / 2x / 4x sweep in docs/SCALING.md

Measured, per run:
    reservations/s      calls created (permit unit + borrower lease + agent reservation + call row)
    reservation latency the dispatch transaction for one call, timed inside each worker
    lock waits          pg_stat_activity sampled every 250 ms: backends waiting on a heavyweight lock
                        (lock-wait seconds = waiting backends x sample interval, an estimate)
    event lag           provider_events.processed_at - received_at
    DB connections      pg_stat_activity, peak during the run
Then the invariant checks run over the history tables.
"""
import argparse
import collections
import json
import os
import statistics
import tempfile
import threading
import time

from demos.common import control, fresh_db, services, start_worker, stop
from demos.setup import campaign
from smartdialer.db import connect
from smartdialer.invariants import check_all

SAMPLE_S = 0.25


class DbSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stopped = threading.Event()
        self.samples = []   # (connections, active, lock_waiting)
        self.waits_by_query = collections.Counter()   # what the lock-waiting backends were running

    def run(self):
        with connect() as conn:
            while not self.stopped.wait(SAMPLE_S):
                r = conn.execute("""SELECT count(*) AS conns,
                                           count(*) FILTER (WHERE state = 'active') AS active,
                                           count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting
                                    FROM pg_stat_activity WHERE datname = current_database()""").fetchone()
                self.samples.append((r["conns"], r["active"], r["waiting"]))
                for w in conn.execute("""SELECT regexp_replace(left(query, 60), '[[:space:]]+', ' ', 'g') AS q FROM pg_stat_activity
                                         WHERE wait_event_type = 'Lock' AND datname = current_database()"""):
                    self.waits_by_query[w["q"]] += 1


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def run_once(n_workers: int, seconds: float, agents: int, borrowers: int) -> dict:
    conn = fresh_db()
    campaign(conn, agents=agents, borrowers=borrowers, settings={"max_attempts": 1})
    with services(webhook_workers=4, agent_wrap_up_s=0.5), tempfile.TemporaryDirectory() as tmp:
        # Provider A as specified (1% explicit errors). time_scale 0.15: ring-out 4.5 s real; talk is
        # 10 s simulated but the mock's min_talk_real_s floor holds it to at least 6 s real, so 1,000
        # agents generate about 150 calls/s. At 16+ workers on one 8-CPU machine the mock's webhook
        # delivery falls behind (docs/SCALING.md), so those numbers include the mock.
        control("A", talk_mean_s=10.0, time_scale=0.15)
        sampler = DbSampler()
        sampler.start()
        files = [os.path.join(tmp, f"w{i}.json") for i in range(n_workers)]
        procs = [start_worker(f"load-{i}", seconds=seconds + 30, quiet=True, extra=["--metrics-file", files[i]])
                 for i in range(n_workers)]
        t0 = time.monotonic()
        time.sleep(seconds)
        stop(*procs)   # SIGTERM: each worker finishes its pass and writes its metrics file
        elapsed = time.monotonic() - t0
        sampler.stopped.set()
        sampler.join()
        lat = [x for f in files if os.path.exists(f) for x in json.load(open(f))["reserve_ms"]]
        time.sleep(3)   # let in-flight provider events land before the invariant pass

    calls = conn.execute("SELECT count(*) AS n FROM calls").fetchone()["n"]
    lag = conn.execute("""SELECT percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP
                              (ORDER BY extract(epoch FROM processed_at - received_at) * 1000) AS p,
                              count(*) FILTER (WHERE processed_at IS NULL) AS unprocessed, count(*) AS total
                          FROM provider_events""").fetchone()
    talk = conn.execute("SELECT count(*) AS n FROM calls WHERE connected_at IS NOT NULL").fetchone()["n"]
    ticks = conn.execute("""SELECT avg(gap) AS avg, max(gap) AS max FROM (
                                SELECT extract(epoch FROM at - lag(at) OVER (ORDER BY id)) * 1000 AS gap
                                FROM safety_decisions) g""").fetchone()
    permits = conn.execute("SELECT sum(approved_count) AS approved, sum(consumed_count) AS claimed FROM dial_permits").fetchone()
    s = sampler.samples
    # settle_checks=False: workers were stopped mid-flight, so some calls are legitimately unfinished.
    violations = [(name, n) for name, n in check_all(conn, settle_checks=False) if n]
    return {
        "workers": n_workers, "seconds": round(elapsed, 1), "calls": calls, "connected": talk,
        "reservations_per_s": round(calls / elapsed, 1),
        "reserve_ms_p50": round(pct(lat, 50), 2), "reserve_ms_p95": round(pct(lat, 95), 2),
        "reserve_ms_p99": round(pct(lat, 99), 2),
        "lock_waiting_avg": round(statistics.mean(x[2] for x in s), 2) if s else 0,
        "lock_waiting_max": max((x[2] for x in s), default=0),
        "lock_wait_s_est": round(sum(x[2] for x in s) * SAMPLE_S, 1),
        "active_backends_avg": round(statistics.mean(x[1] for x in s), 1) if s else 0,
        "db_connections_max": max((x[0] for x in s), default=0),
        "event_lag_ms_p50": round(lag["p"][0], 1) if lag["p"] else None,
        "event_lag_ms_p95": round(lag["p"][1], 1) if lag["p"] else None,
        "event_lag_ms_p99": round(lag["p"][2], 1) if lag["p"] else None,
        "events": lag["total"], "events_unprocessed": lag["unprocessed"],
        "pacing_tick_gap_ms_avg": round(ticks["avg"] or 0), "pacing_tick_gap_ms_max": round(ticks["max"] or 0),
        "units_approved": permits["approved"], "units_claimed": permits["claimed"],
        "lock_waits_by_query": dict(sampler.waits_by_query.most_common(3)),
        "invariant_violations": violations,
    }


COLS = [("workers", "workers"), ("reservations_per_s", "res/s"), ("reserve_ms_p50", "p50 ms"),
        ("reserve_ms_p95", "p95 ms"), ("reserve_ms_p99", "p99 ms"), ("lock_waiting_avg", "lock-wait avg"),
        ("lock_wait_s_est", "lock-wait s"), ("event_lag_ms_p50", "ev lag p50"), ("event_lag_ms_p99", "ev lag p99"),
        ("db_connections_max", "conns"), ("pacing_tick_gap_ms_avg", "tick gap ms"), ("invariant_violations", "violations")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, nargs="+", default=[8])
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--borrowers", type=int, default=50_000)
    args = ap.parse_args()
    results = []
    for n in args.workers:
        print(f"\n=== {n} workers, {args.seconds:.0f} s ===", flush=True)
        r = run_once(n, args.seconds, args.agents, args.borrowers)
        print(json.dumps(r, indent=2), flush=True)
        results.append(r)
    os.makedirs("results/loadtest", exist_ok=True)
    with open("results/loadtest/results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\n| " + " | ".join(h for _, h in COLS) + " |\n|" + "---|" * len(COLS))
    for r in results:
        print("| " + " | ".join(str(len(r[k]) if k == "invariant_violations" else r[k]) for k, _ in COLS) + " |")
    ok = all(not r["invariant_violations"] for r in results)
    print("\nRESULT:", "PASS (all invariants hold)" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
