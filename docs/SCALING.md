# Scaling

All current numbers here are measured on the current code by `make loadtest ARGS="--workers 8 16 32"`
(`loadtest/run.py`, raw output in `results/loadtest/results.json`, which is local and not committed).
Each run is a fresh database with **1,000 agents, 50,000 borrowers, one progressive campaign,
Provider A, 60 s**.

## Setup and its limits

- Everything runs on one machine: Docker Desktop with 8 CPUs, shared by Postgres, the mock
  provider, the webhook receiver (4 processes) and N worker processes.
- Talk time: the load test asks for 10 s simulated at `time_scale` 0.15 (1.5 s real), but the mock now
  holds every answered call for at least `min_talk_real_s` = 6 s of real time, so a borrower never hangs
  up faster than a dialer could bridge them. Ring-out is 4.5 s. An agent's cycle is therefore about
  6.5 s on average, so 1,000 agents generate about **150 calls/s** of demand. That is still far more than
  real traffic (see below).
- Reservation latency is the time to create one call, timed in the worker: the permit work (a claim when
  the worker has no units left, then the pin), borrower lease, agent reservation, call insert, commit.
  Lock waits are sampled from `pg_stat_activity` every 250 ms. "Lock-wait s" is waiting backends x interval,
  an estimate.
- **The mock provider is part of the measurement at 16 and 32 workers.** Its webhook delivery competes for
  the same 8 CPUs. At 32 workers, ANSWERED events reached the inbox a median **8.3 s** (p90 10.7 s) after
  the provider sent them, while processing them took 0.04 s. See "Overload turns into abandons" below.

## Results (current code)

| Workers | Reservations/s | p50 ms | p95 ms | p99 ms | Backends waiting on a lock (avg / max) | Lock-wait s (est.) | Event lag p50 / p99 ms | Connected calls | DB connections (peak) | Invariant violations |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | **78.6** | 10.6 | 32 | 52 | 0.08 / 3 | 5 | 154 / 906 | 1,324 | 45 | 0 |
| 16 | **77.5** | 16.8 | 73 | 171 | 0.51 / 10 | 31 | 79 / 1,360 | 322 | 54 | 0 |
| 32 | **50.0** | 16.4 | 77 | 223 | 2.78 / 25 | 195 | 51 / 4,786 | 38 | 68 | 0 |

Event lag is `processed_at - received_at` in the inbox, so it does not include webhook delivery.

All invariants held in every run: no borrower with overlapping calls, no agent bound to two calls,
every transition legal and chained, no duplicate transitions, permits never over-claimed, and calls per
permit never more than claimed units.

### Historical: the batch-claims fix (commit f550fcc)

Measured when batch claims were introduced, on the code and mock of that commit (1.5 s real talk time,
about 250 calls/s of demand, no talk floor, no late-answer rule). **Not comparable** to the table above;
kept because it is the evidence for ADR-4. The "before" code no longer exists, so it cannot be rerun.

| Workers | | Reservations/s | p50 ms | p95 ms | p99 ms | Backends waiting on a lock (avg / max) | Lock-wait s (est.) | Event lag p50 / p99 ms |
|---|---|---|---|---|---|---|---|---|
| 8 | before | 108.0 | 18 | 70 | 120 | 2.2 / 7 | 134 | 97 / 965 |
| 8 | after | 165.8 | 4.6 | 12 | 22 | 0.07 / 4 | 4 | 39 / 400 |
| 16 | before | 123.3 | 41 | 188 | 323 | 7.1 / 15 | 431 | 247 / 1,547 |
| 16 | after | 157.8 | 5.3 | 14 | 25 | 0.34 / 11 | 21 | 7 / 206 |
| 32 | before | 78.5 | 117 | 516 | 831 | 12.4 / 31 | 787 | 493 / 2,400 |
| 32 | after | 138.8 | 7.4 | 24 | 46 | 2.4 / 27 | 142 | 5 / 199 |

## Before: the permit row was the bottleneck (historical)

Doubling workers from 8 to 16 bought only 14% more throughput. Doubling again to 32 *lost* 36% compared
to 8, while p50 reservation latency rose 6.4x. A lock sampler run during a 32-worker load showed:

| Waiting on | Samples |
|---|---|
| `transactionid` lock from `UPDATE dial_permits ... consumed_count + 1` | 602 |
| `tuple` lock on `dial_permits` from the same UPDATE | 77 |
| anything else | 0 |

Every lock wait was on the campaign's permit row. `consume_one` was the first statement of the
dispatch transaction, so the row stayed locked through the borrower lease, the agent reservation, the
call insert and the COMMIT. Dispatch for one campaign was therefore **serial**, with a ceiling of
1 / (time to run that transaction), about 8 to 9 ms, so about 110 to 120 reservations/s. That is
exactly where the 8 and 16 worker runs topped out. Event lag degraded second, because a worker
queued on the permit row is not processing events.

The load test also found a deadlock, now fixed. The Safety Controller locked the campaign row `FOR UPDATE` and then
waited on the permit row. A consumer held the permit row and then inserted a call, whose foreign key check
needs `KEY SHARE` on the campaign row. The fix is `FOR NO KEY UPDATE` (PLAN D10), covered by
`tests/test_permits.py::test_decider_waits_for_pinned_calls_and_counts_them`.

## The fix: batch claims (ADR-4)

A worker claims up to 10 units with one statement in its own transaction
(`SafetyController.claim_units`), so the permit row lock lasts one statement. Each call transaction
then **pins** the permit with `FOR KEY SHARE` (`SafetyController.pin`). That checks the permit is still
live, and pins conflict neither with each other nor with claims. Units a worker claimed but did not use
are dropped when the permit is revoked.

Measured at commit f550fcc (historical table above):

- p50 reservation latency: 18 / 41 / 117 ms before, 4.6 / 5.3 / 7.4 ms after.
- Throughput: +54% at 8 workers, +28% at 16, +77% at 32.
- Waiting backends at 8 workers: 2.2 before, 0.07 after.
- Event lag p99 at 32 workers: 2.4 s before, 0.2 s after, because workers no longer queue on the permit row.

## Now: what limits the current code

Throughput is about 78 reservations/s at 8 and 16 workers and 50/s at 32, against about 150/s of
demand, with hundreds of agents AVAILABLE (743 at the end of the 32-worker run).

| Workers | Pacing tick gap avg / max (target 250 ms) | Units approved | Units claimed | Calls |
|---|---|---|---|---|
| 8 | 1,801 / 3,718 ms | 15,482 | 5,307 | 4,808 |
| 16 | 1,108 / 4,177 ms | 6,787 | 5,042 | 4,883 |
| 32 | 1,021 / 4,647 ms | 4,012 | 3,662 | 3,656 |

- **Pacing tick cadence.** The leader is an ordinary worker. It runs the pacing tick in the same loop as
  dispatch, provider dials and hang-ups (up to 3 s each), event processing and reconciliation. Its ticks
  now arrive every 1.0 to 1.8 s on average instead of 250 ms (the batch-claims sweep measured 340 to 570 ms).
  An agent who frees up just after a tick waits for the next one, and each progressive permit expires 2 s
  after it is issued.
- **At 8 workers**, workers cannot spend what is approved: 66% of approved units are never claimed before
  the permit is replaced. The limit is dispatch capacity (8 Python loops that also do provider I/O),
  plus the leader's slow ticks.
- **At 16 and 32 workers**, approvals themselves shrink, and the claim statement queues: 2.8 waiting backends
  on average at 32 workers, nearly all on `UPDATE dial_permits ... consumed_count + k`.

### Overload turns into abandons

Connected calls fall from 1,324 at 8 workers to 322 at 16 and 38 at 32. At 32 workers, 1,034 of 1,072
answered calls ended `ABANDONED (ANSWER_PROCESSED_TOO_LATE)`. The cause is webhook delivery, not the
dialer's processing: ANSWERED reached the inbox a median 8.3 s after the provider sent it, because the mock's
delivery threads, 32 workers and Postgres share 8 CPUs. By the time a worker applies the answer, the 2 s
hold is long gone, so the call is recorded as an abandon and hung up. That is the correct outcome: the
borrower answered and nobody was bridged for 8 s. The previous sweep, before the late-answer rule, recorded
such calls as CONNECTED. Each of these hang-ups is one more request to the already overloaded provider,
so the overload feeds itself. In production the equivalent is a slow or lagging webhook path, and the
abandon budget would then force progressive dialing. Measuring the dialer alone at this rate needs the
mock on separate hardware.

Proposed fix, **NOT BUILT** (feature freeze): give the pacing leader and event processing their own loops,
apart from provider I/O (fix 2 below), then shard the permit (fix 3) to remove the last shared row from
the dispatch path.

## Why "add more servers" makes it worse

Throughput is bounded by shared rows and a single decider. Adding workers does not add capacity to a row.
It adds waiters:

- Before the fix, every extra worker queued on the permit lock while holding a connection, the lock
  holder ran slower on a busier CPU, and throughput fell from 123/s to 78/s (historical sweep).
- On the current code the same shape remains: 79/s at 8 workers, 50/s at 32. More claimers
  queue on the (now short) claim statement, and more processes slow down the leader's tick.
- Workers stuck on a lock are not processing events, so event lag rises too (before: 97 ms to 493 ms p50).

The fix is always to remove the shared hot row, or the single serial step, from the path, not to add
processes behind it.

## 100, 1,000, 10,000 agents

Real traffic is much lighter than the load test, and how much lighter depends on the answer rate.
Every connected call occupies an agent for talk plus wrap-up, and only a fraction p of dials connect. So
the dial rate needed to keep N agents busy at utilization U is:

```
dials/s = N x U / (p x (T_talk + T_wrap))
```

With U = 100% this is the upper bound, which is what a perfect predictive dialer would need. Unanswered calls do not appear in the
formula because they ring without occupying an agent (predictive); in progressive mode they do occupy
one, which lowers U and therefore the dial rate. For 1,000 agents, with the simulator's scenarios
(`sim/scenarios.py`, wrap-up 15 s):

| Scenario | p | T_talk | dials/s at U = 100% (upper bound) | dials/s at the simulator's measured predictive U |
|---|---|---|---|---|
| A | 20% | 120 s | 1000 / (0.20 x 135) = **37.0** | 37.0 x 64.7% = 24.0 |
| B | 50% | 90 s | 1000 / (0.50 x 105) = **19.0** | 19.0 x 70.4% = 13.4 |
| C | 70% | 180 s | 1000 / (0.70 x 195) = **7.3** | 7.3 x 88.2% = 6.5 |

So 1,000 agents need **at most about 37 dials/s**, at the lowest answer rate (A), and the rate scales linearly
with N. Each dial produces 3 to 4 inbox events. Measured per-campaign throughput on the current code, on one 8-CPU
machine with the mock provider sharing it: about 78 reservations/s (8 and 16 workers), 50/s at 32.

| Agents | Dials/s (C to A, upper bound) | Inbox events/s | What breaks first |
|---|---|---|---|
| 100 | 0.7 to 3.7 | up to about 13 | Nothing. One worker is enough; more workers are for crash tolerance |
| 1,000 | 7 to 37 | up to about 130 | Nothing in one campaign: 37/s is under half of the measured 78/s. The `provider_health` row (C5) and the per-tick snapshot query start to show up if there are many campaigns |
| 10,000 | 73 to 370 | up to about 1,300 | **Per-campaign permit supply**, if those agents are in one campaign at a low answer rate: A needs 370/s and B 190/s, against about 78/s measured (pacing tick cadence, then the claim row). C (73/s) is at the limit. Next: inbox write volume and connection count as workers grow |
| 100,000 | 730 to 3,700 | up to about 13,000 | Single Postgres write throughput for transitions plus inbox, connection count, and the inbox table size |

## Fixes, in the order I would apply them

1. **Done: batched permit claims** (above, ADR-4). The alternative considered first was to take the permit unit last in
   the call transaction. That shortens the lock but still takes it once per call. Batching takes it once
   per 10 calls and keeps the existing lock order.
2. **A dedicated pacing loop. NOT BUILT.** Run the leader's tick on its own thread or process, so it is never
   behind a 3 s provider call or a batch of events. Ticks then arrive every 250 ms and permits track
   agents as they free up. This is the measured bottleneck now.
3. **Permit shards. NOT BUILT.** The Safety Controller splits an approval of N into S rows of N/S each, and
   each worker claims from shard `hash(worker) % S`, falling back to others when its shard is empty.
   The total stays exactly N (the sum of approvals), and claim contention drops by S.
4. **Partition by campaign or agent pool.** Each campaign already has its own permit row, leader and
   snapshot. Running a large operation as several campaigns of 1,000 to 2,000 agents, and assigning
   workers to campaigns (instead of every worker scanning every campaign), means workers stop
   competing for the same rows at all.
5. **Provider health (C5).** Today `provider_health` is one row per provider, rewritten by every
   campaign leader's breaker evaluation, which makes it a write hotspot as campaigns multiply. Fix: one health row per
   (provider, campaign) or per worker, with OPEN if any shard says OPEN, aggregated on read. Or compute
   health on read from a time-bucketed counter table (`provider_attempts(provider, minute, ok, failed)`)
   that is only incremented. The simple version is kept because one campaign per provider does not stress it.
6. **Connection pooling.** Each worker holds one connection and each webhook process a pool of 10. Past a few
   hundred processes, put PgBouncer in transaction mode in front. Workers use no session state, so transaction
   pooling is safe. The one session advisory lock is the migration runner's, which connects directly, not
   through the pooler.
7. **Partitioned inbox.** `provider_events` is append-heavy and only the unprocessed rows are hot.
   Partition by day (drop old partitions instead of deleting) and keep the partial index on
   unprocessed rows. At 10,000 events/s, hash-partition by `call_id` so per-call FIFO stays within a
   partition and workers can claim from disjoint partitions.
8. **Single pacing leader per campaign.** It is a deliberate serialization point: one decider per
   campaign is what makes the permit math exact. After batching, its *cadence* is the bottleneck
   (fix 2). Its *throughput* is not: about 4 decisions/s, one transaction each. Scale it by giving it
   its own loop and by having more campaigns, not more leaders.
9. **Snapshot query.** It aggregates over the campaign's recent calls on every tick. At 10,000 agents
   in one campaign, keep running counters per campaign (updated in the transition transaction), or
   read from a replica with the staleness rule (STALE_SNAPSHOT) already in place.
10. **A breaker that can see at low traffic. NOT BUILT.** Finding from the provider_outage demo: the
   breaker is driven only by live call outcomes (the last 20 attempts since its last state change).
   When something else throttles dialing, for example the abandon budget forcing progressive at 1 call
   per tick, too few attempts reach the provider to fill the window, and the breaker stays CLOSED
   through an outage it cannot see. Low volume is exactly when it goes blind. Proposed fix: a minimum
   volume before judging (below N attempts, report UNKNOWN, not CLOSED, and treat UNKNOWN like DEGRADED),
   or an active health probe (a cheap periodic status call per provider, fed into the same window), so
   provider health no longer depends on how much the dialer happens to be dialing.
