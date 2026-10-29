# Scaling

All numbers here are measured by `make loadtest ARGS="--workers 8 16 32"` (`loadtest/run.py`,
raw output in `results/loadtest/results.json`). Each run is a fresh database with
**1,000 agents, 50,000 borrowers, one progressive campaign, Provider A, 60 s**.

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
- Reservation latency is the dispatch transaction for one call, timed in the worker: permit unit,
  borrower lease, agent reservation, call insert, commit. Lock waits are sampled from
  `pg_stat_activity` every 250 ms. "Lock-wait s" is waiting backends x interval, an estimate.

## Results

| Workers | Reservations/s | p50 ms | p95 ms | p99 ms | Backends waiting on a lock (avg / max) | Lock-wait s (est.) | Event lag p50 / p99 ms | DB connections (peak) | Invariant violations |
|---|---|---|---|---|---|---|---|---|---|
| 8 | **108.0** | 18 | 70 | 120 | 2.2 / 7 | 134 | 97 / 965 | 40 | 0 |
| 16 | **123.3** | 41 | 188 | 323 | 7.1 / 15 | 431 | 247 / 1,547 | 45 | 0 |
| 32 | **78.5** | 117 | 516 | 831 | 12.4 / 31 | 787 | 493 / 2,400 | 66 | 0 |

All invariants held in every run: no borrower with overlapping calls, no agent bound to two calls,
every transition legal and chained, no duplicate transitions, permits never over-consumed, calls per
permit equal to consumed units. Event lag is `processed_at - received_at` in the inbox.

## What degraded first

**The permit row.** Doubling workers from 8 to 16 bought only 14% more throughput. Doubling again to 32
*lost* 36% compared to 8, while p50 reservation latency rose 6.4x. A lock sampler run during a
32-worker load showed:

| Waiting on | Samples |
|---|---|
| `transactionid` lock from `UPDATE dial_permits ... consumed_count + 1` | 602 |
| `tuple` lock on `dial_permits` from the same UPDATE | 77 |
| anything else | 0 |

Every lock wait was on the campaign's permit row. `consume_one` is the first statement of the
dispatch transaction, so the row stays locked through the borrower lease, the agent reservation, the
call insert and the COMMIT. Dispatch for one campaign is therefore **serial**. The ceiling is
1 / (time to run that transaction), about 8 to 9 ms, so about 110 to 120 reservations/s. That is
exactly where the 8 and 16 worker runs top out.

**Event lag degraded second**, as a consequence. Workers run the event processor in the same loop,
and a worker queued on the permit row is not processing events. p50 event lag went 97 ms to 493 ms.

**Connections were not the problem** at this scale: 66 at peak against `max_connections` 400.

The load test also found a bug, now fixed. The Safety Controller locked the campaign row `FOR UPDATE` and then
waited on the permit row. A consumer held the permit row and then inserted a call, whose foreign key check
needs `KEY SHARE` on the campaign row. That was a deadlock. The fix is `FOR NO KEY UPDATE` (PLAN D10), covered by
`tests/test_permits.py::test_decider_and_consumer_do_not_deadlock`.

## Why "add more servers" makes it worse

Throughput here is bounded by one row lock. Adding workers does not add capacity to a row. It adds
waiters:

- Every extra worker queues on the same lock while holding a connection.
- When the holder commits, Postgres wakes waiters one by one. Each re-checks the row (it was updated)
  and the next one takes the lock. The handoff cost grows with the queue.
- More processes compete for the same CPUs, so the transaction that holds the lock runs slower. The lock
  is held longer, and throughput falls. That is the drop from 123/s to 78/s.
- Workers stuck on the lock are not processing events, so event lag rises too.

The fix is to remove the shared hot row from the path, not to add processes behind it.

## 100, 1,000, 10,000 agents

Real traffic is much lighter than the load test. A progressive agent with about 25 s of ringing, 30% answer
rate and about 3.5 minutes of talk plus wrap-up makes about one dial per 90 s, roughly 0.011 dials/s.
Predictive at its 2.5x hard cap makes about 0.03 dials/s. Each call produces 3 to 4 inbox events.

| Agents | Dials/s (progressive to predictive cap) | Inbox events/s | What breaks first |
|---|---|---|---|
| 100 | 1 to 3 | about 10 | Nothing. One worker is enough; more workers are for crash tolerance |
| 1,000 | 11 to 30 | about 100 | Nothing in one campaign: 30/s is a quarter of the measured permit-row ceiling. The `provider_health` row (C5) and the per-tick snapshot query start to show up if there are many campaigns |
| 10,000 | 110 to 300 | about 1,000 | **The permit row**, if those agents are in one campaign: 300/s is 2.5x the measured ceiling. Next: inbox write volume and connection count as workers grow |
| 100,000 | 1,100 to 3,000 | about 10,000 | Single Postgres write throughput for transitions plus inbox, connection count, and the inbox table size |

## Fixes, in the order I would apply them

1. **Take the permit unit last.** Reorder dispatch to borrower lease, agent reserve, call insert, then
   `consume_one` right before COMMIT. The hot lock is then held for one statement plus commit instead
   of five, which should raise the per-campaign ceiling severalfold. The cost is that the lock order
   (C3) changes to borrower, agent, permit for dispatch. The decider never locks borrowers or agents, so no
   cycle appears, but this needs a deadlock test like D10's.
2. **Batched reservations.** Consume k units in one statement
   (`consumed_count = consumed_count + k ... RETURNING`), then lease k borrowers and reserve k agents with
   `SKIP LOCKED LIMIT k`. One permit lock per batch instead of per call.
3. **Permit shards.** The Safety Controller splits an approval of N into S rows of N/S each, and
   each worker consumes from shard `hash(worker) % S`, falling back to others when its shard is empty.
   The total stays exactly N (the sum of approvals), and contention drops by S.
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
   hundred processes, put PgBouncer in transaction mode in front. Workers use no session state, so transaction pooling is safe. The one session advisory
   lock is the migration runner's, which connects directly, not through the pooler.
7. **Partitioned inbox.** `provider_events` is append-heavy and only the unprocessed rows are hot.
   Partition by day (drop old partitions instead of deleting) and keep the partial index on
   unprocessed rows. At 10,000 events/s, hash-partition by `call_id` so per-call FIFO stays within a
   partition and workers can claim from disjoint partitions.
8. **Single pacing leader per campaign is not the bottleneck.** It makes about 4 decisions/s per
   campaign, and each one is one transaction. It is a deliberate serialization point: one decider per campaign is
   what makes the permit math exact. Scale it by having more campaigns, not more leaders.
9. **Snapshot query.** It aggregates over the campaign's recent calls on every tick. At 10,000 agents
   in one campaign, keep running counters per campaign (updated in the transition transaction), or
   read from a replica with the staleness rule (STALE_SNAPSHOT) already in place.
