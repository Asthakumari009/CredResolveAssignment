# Scaling

All numbers here are measured by `make loadtest ARGS="--workers 8 16 32"` (`loadtest/run.py`).
Each run is a fresh database with **1,000 agents, 50,000 borrowers, one progressive campaign,
Provider A, 60 s**.

## Setup and its limits

- Everything runs on one machine: Docker Desktop with 8 CPUs, shared by Postgres, the mock
  provider, the webhook receiver (4 processes) and N worker processes.
- Talk time is about 1.5 s real (10 s simulated at `time_scale` 0.15) and ring-out is 4.5 s, so 1,000
  agents generate about 250 calls/s of demand. That is far more than real traffic (see below).
- The mock provider's own ceiling was measured first, so its limits are not mistaken for the dialer's. It does
  about 1,100 dials/s without events and about 300 calls/s with webhooks. Webhook delivery originally used
  httpx's async client at about 70 events/s, which made the mock the bottleneck. It was switched
  to a thread pool at about 600 events/s (PLAN D11). In every run below, webhook delivery lag (provider
  timestamp to inbox) stayed under 0.2 s and no dial timed out, so the provider was not the constraint.
- Reservation latency is the time to create one call, timed in the worker: the permit work (a claim when
  the worker has no units left, then the pin), borrower lease, agent reservation, call insert, commit.
  Lock waits are sampled from `pg_stat_activity` every 250 ms. "Lock-wait s" is waiting backends x interval,
  an estimate.

## Results

Two sweeps: **before**, where each call's transaction locked the permit row (`consume_one`), and
**after**, where units are claimed in batches (ADR-4). Raw output: `results/loadtest/before_batch_claims.json`
and `results/loadtest/results.json`.

| Workers | | Reservations/s | p50 ms | p95 ms | p99 ms | Backends waiting on a lock (avg / max) | Lock-wait s (est.) | Event lag p50 / p99 ms | DB connections (peak) | Invariant violations |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | before | 108.0 | 18 | 70 | 120 | 2.2 / 7 | 134 | 97 / 965 | 40 | 0 |
| 8 | **after** | **165.8** | **4.6** | 12 | 22 | 0.07 / 4 | 4 | 39 / 400 | 36 | 0 |
| 16 | before | 123.3 | 41 | 188 | 323 | 7.1 / 15 | 431 | 247 / 1,547 | 45 | 0 |
| 16 | **after** | **157.8** | **5.3** | 14 | 25 | 0.34 / 11 | 21 | 7 / 206 | 51 | 0 |
| 32 | before | 78.5 | 117 | 516 | 831 | 12.4 / 31 | 787 | 493 / 2,400 | 66 | 0 |
| 32 | **after** | **138.8** | **7.4** | 24 | 46 | 2.4 / 27 | 142 | 5 / 199 | 66 | 0 |

The "after" sweep was run twice. The first run gave 157.9 / 169.9 / 125.0 reservations/s, so treat
differences under about 10% between worker counts as noise.

All invariants held in every run: no borrower with overlapping calls, no agent bound to two calls,
every transition legal and chained, no duplicate transitions, permits never over-claimed, and calls per
permit never more than claimed units. Event lag is `processed_at - received_at` in the inbox.

## Before: the permit row was the bottleneck

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

- p50 reservation latency: 18 / 41 / 117 ms before, 4.6 / 5.3 / 7.4 ms after.
- Throughput: +54% at 8 workers, +28% at 16, +77% at 32.
- Waiting backends at 8 workers: 2.2 before, 0.07 after.
- Event lag p99 at 32 workers: 2.4 s before, 0.2 s after, because workers no longer queue on the permit row.
- Units dropped (claimed, never a call): 423 of 10,667 (4.0%) at 8 workers, 71 of 9,803 (0.7%) at 16, and
  18 of 8,488 (0.2%) at 32. They only ever mean fewer calls, never more.

## After: what is the bottleneck now

Throughput is now flat at about 140 to 170 reservations/s from 8 to 32 workers, while demand is about
250/s and hundreds of agents sit AVAILABLE. Lock waits do not limit it at 8 or 16 workers.
The new limit is **permit supply from the single pacing leader**:

| Workers | Pacing tick gap avg / max (target 250 ms) | Units approved | Units claimed | Calls |
|---|---|---|---|---|
| 8 | 568 / 2,315 ms | 14,705 | 10,667 | 10,244 |
| 16 | 336 / 611 ms | 11,034 | 9,803 | 9,732 |
| 32 | 439 / 1,142 ms | 9,647 | 8,488 | 8,470 |

- The leader is an ordinary worker. It runs the pacing tick in the same loop as dispatch, provider dials
  (up to 3 s), event processing and reconciliation. So its ticks arrive every 340 to 570 ms instead of 250 ms.
  An agent who frees up just after a tick waits for the next one. Each progressive permit approves about
  the agents available at that instant, and it expires 2 s later.
- **At 8 workers**, workers cannot spend what is approved: 27% of approved units are never claimed before
  the permit is replaced. The limit is dispatch capacity (8 Python loops), plus the leader's slow ticks.
- **At 32 workers**, approvals themselves shrink (9,647). Ticks slow down again because 32 worker
  processes, Postgres, the mock and the webhook share 8 CPUs. The claim statement also has a queue now: 2.4 waiting
  backends on average, all on `UPDATE dial_permits ... consumed_count + k`. The lock is short, but it is still one row.

So the next fixes are: give the pacing leader its own loop (a thread or process that only paces, so ticks
are on time), and shard the permit (fix 3 below) to remove the last shared row from the dispatch path.

## Why "add more servers" makes it worse

Throughput is bounded by shared rows and a single decider. Adding workers does not add capacity to a row.
It adds waiters:

- Before the fix, every extra worker queued on the permit lock while holding a connection, the lock
  holder ran slower on a busier CPU, and throughput fell from 123/s to 78/s.
- After the fix, the same shape remains at a higher level: 166/s at 8 workers, 139/s at 32. More claimers
  queue on the (now short) claim statement, and more processes slow down the leader's tick.
- Workers stuck on a lock are not processing events, so event lag rises too (before: 97 ms to 493 ms p50).

The fix is always to remove the shared hot row, or the single serial step, from the path, not to add
processes behind it.

## 100, 1,000, 10,000 agents

Real traffic is much lighter than the load test. A progressive agent with about 25 s of ringing, 30% answer
rate and about 3.5 minutes of talk plus wrap-up makes about one dial per 90 s, roughly 0.011 dials/s.
Predictive at its 2.5x hard cap makes about 0.03 dials/s. Each call produces 3 to 4 inbox events.

| Agents | Dials/s (progressive to predictive cap) | Inbox events/s | What breaks first |
|---|---|---|---|
| 100 | 1 to 3 | about 10 | Nothing. One worker is enough; more workers are for crash tolerance |
| 1,000 | 11 to 30 | about 100 | Nothing in one campaign: 30/s is a fifth of the measured per-campaign throughput. The `provider_health` row (C5) and the per-tick snapshot query start to show up if there are many campaigns |
| 10,000 | 110 to 300 | about 1,000 | **Per-campaign permit supply**, if those agents are in one campaign: 300/s is about 2x the measured 140 to 170/s (pacing tick cadence, then the claim row). Next: inbox write volume and connection count as workers grow |
| 100,000 | 1,100 to 3,000 | about 10,000 | Single Postgres write throughput for transitions plus inbox, connection count, and the inbox table size |

## Fixes, in the order I would apply them

1. **Done: batched permit claims** (above, ADR-4). The alternative considered first was to take the permit unit last in
   the call transaction. That shortens the lock but still takes it once per call. Batching takes it once
   per 10 calls and keeps the existing lock order.
2. **A dedicated pacing loop.** Run the leader's tick on its own thread or process, so it is never
   behind a 3 s provider call or a batch of events. Ticks then arrive every 250 ms and permits track
   agents as they free up. This is the measured bottleneck now.
3. **Permit shards.** The Safety Controller splits an approval of N into S rows of N/S each, and
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
