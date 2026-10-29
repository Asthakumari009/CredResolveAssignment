# Scaling

Current numbers are measured on the current code by `make loadtest` (8 workers; `loadtest/run.py`, raw
output in `results/loadtest/results.json`, which is local and not committed).

**Read this first: the mock provider, the webhook receiver, Postgres and the dialer workers all share the
same 8 CPUs. Runs above 8 workers measure the test harness, not the dialer.** Past 8 workers the mock's
event delivery is starved of CPU, events arrive seconds late, and what the numbers show is the Safety
Controller correctly refusing to dial into that. Only the 8-worker run is a measurement of the dialer.
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
- With the per-tick cap and the recovery ramp in place, the 8-worker run keeps event delivery healthy:
  ANSWERED reached the inbox a median 21 ms after the provider sent it (1.2 s before the cap).

## Results (current code, 8 workers)

| Workers | Reservations/s | p50 ms | p95 ms | p99 ms | Backends waiting on a lock (avg / max) | Lock-wait s (est.) | Inbox lag p50 / p99 ms | DB connections (peak) | Invariant violations |
|---|---|---|---|---|---|---|---|---|---|
| 8 | **102.8** | 6.4 | 19 | 36 | 0.03 / 2 | 2 | 20 / 400 | 45 | 0 |

| Answered | Abandoned | Abandon rate | Decisions | Capped by MAX_PER_TICK | Ramped (RECOVERY_RAMP) | Rejected by EVENT_LAG | Worst event lag |
|---|---|---|---|---|---|---|---|
| 1,876 | 0 | **0.0%** | 127 | 127 | 40 (the first 20 s) | 0 | 481 ms |

Inbox lag is `processed_at - received_at`. Event lag (the EVENT_LAG rule) runs from the provider's timestamp,
so it includes webhook delivery.

All invariants held: no borrower with overlapping calls, no agent bound to two calls, every transition legal
and chained, no duplicate transitions, permits never over-claimed, and calls per permit never more than
claimed units.

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

At 8 workers: 6,350 units approved, 6,350 claimed, 6,350 calls. Every unit approved was used, and every one
of the 127 decisions was capped at 50 by MAX_PER_TICK. **The per-tick cap is now the limit, by design:**
50 calls per tick with ticks every 444 ms on average is at most about 113 calls/s, and the run did 103/s
against about 150/s of demand.

- That is a deliberate trade: no single decision can release a burst the provider and the inbox cannot
  absorb. Raising throughput means ticking on time (250 ms gives 200/s at the same cap, fix 2 below) or raising
  `max_approved_per_tick`, not removing the cap.
- The pacing leader is still an ordinary worker, so its ticks arrive every 444 ms instead of 250 ms (fix 2).

## Late events: EVENT_LAG, the recovery ramp and the per-tick cap

**The gap.** At 32 workers (commit b75107d) ANSWERED reached the inbox a median 8.3 s late, 1,034 of 1,072
answered calls (96%) were abandoned, and the Safety Controller kept approving. ABANDON_BUDGET only forces
progressive, and progressive abandons just as surely when nobody sees the answer in time.

**EVENT_LAG (PLAN D17).** Event lag from the inbox: provider timestamp to applied (or to now, if still waiting),
p90 over the last 10 s, or the age of the oldest unprocessed event, whichever is worse. Above
`max_answer_hold_ms` (2 s) it REJECTS every dial, progressive included. Rerun at commit 1b35c59, before the
ramp and cap (16 and 32 workers measure the harness, see the top of this page, but they show the rule's behaviour):

| Workers | Abandon rate | Decisions rejected by EVENT_LAG |
|---|---|---|
| 8 | 2.6% | 36 of 89 |
| 16 | 38.5% | 106 of 135 |
| 32 | 92.2% | 116 of 122 |

It was reactive, and at 32 workers the decision log showed why it still lost most answers: the opening burst
(1,000 + 651 + 145 approvals in 5 s) went out before any event was late enough to see, and when the lag
window cleared, progressive released all 1,000 available agents in one tick and overloaded delivery again.

**RECOVERY_RAMP and MAX_PER_TICK (PLAN D18)** close both holes:

- After startup or any pause in dialing (a REJECT for EVENT_LAG, PROVIDER_HEALTH or the calling window,
  HALF_OPEN probes, an AGENT_DROP cooldown), approvals ramp from 20% to 100% over 20 s.
- No decision approves more than `max_approved_per_tick` (default 50), in any mode, progressive and
  COLD_START included.

Effect at 8 workers: abandon rate 2.6% to 0.0%, EVENT_LAG rejects 36 to 0, ANSWERED delivery lag 1.2 s to
21 ms, throughput 86 to 103 reservations/s. Bounding the burst kept delivery healthy, which kept every answer
on time.

## Why "add more servers" makes it worse

Throughput is bounded by shared rows and a single decider. Adding workers does not add capacity to a row.
It adds waiters:

- Before the fix, every extra worker queued on the permit lock while holding a connection, the lock
  holder ran slower on a busier CPU, and throughput fell from 123/s to 78/s (historical sweep).
- At commit 1b35c59, adding workers past 8 lowered throughput from 86/s to 33/s at 32. On this machine that
  measures the harness: more processes on the same 8 CPUs starve the mock's event delivery, and EVENT_LAG
  then holds dialing.
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
| A | 20% | 120 s | 1000 / (0.20 x 135) = **37.0** | 37.0 x 64.4% = 23.8 |
| B | 50% | 90 s | 1000 / (0.50 x 105) = **19.0** | 19.0 x 70.3% = 13.4 |
| C | 70% | 180 s | 1000 / (0.70 x 195) = **7.3** | 7.3 x 87.3% = 6.4 |

So 1,000 agents need **at most about 37 dials/s**, at the lowest answer rate (A), and the rate scales linearly
with N. Each dial produces 3 to 4 inbox events. Measured per-campaign throughput on the current code, on one 8-CPU
machine with the mock provider sharing it: about 103 reservations/s at 8 workers, held there by the per-tick
cap (50 per tick).

| Agents | Dials/s (C to A, upper bound) | Inbox events/s | What breaks first |
|---|---|---|---|
| 100 | 0.7 to 3.7 | up to about 13 | Nothing. One worker is enough; more workers are for crash tolerance |
| 1,000 | 7 to 37 | up to about 130 | Nothing in one campaign: 37/s is about a third of the measured 103/s. The `provider_health` row (C5) and the per-tick snapshot query start to show up if there are many campaigns |
| 10,000 | 73 to 370 | up to about 1,300 | **Per-campaign permit supply**, if those agents are in one campaign at a low answer rate: A needs 370/s and B 190/s, against about 103/s measured (the per-tick cap at the current tick cadence, then the claim row). C (73/s) fits.
Event delivery must keep up too, or EVENT_LAG holds dialing. Next: inbox write volume and connection count as workers grow |
| 100,000 | 730 to 3,700 | up to about 13,000 | Single Postgres write throughput for transitions plus inbox, connection count, and the inbox table size |

## Fixes, in the order I would apply them

1. **Done: batched permit claims** (above, ADR-4). The alternative considered first was to take the permit unit last in
   the call transaction. That shortens the lock but still takes it once per call. Batching takes it once
   per 10 calls and keeps the existing lock order.
2. **A dedicated pacing loop. NOT BUILT.** Run the leader's tick on its own thread or process, so it is never
   behind a 3 s provider call or a batch of events. Ticks then arrive every 250 ms and permits track
   agents as they free up. With the per-tick cap of 50, throughput is cap / tick gap, so on-time ticks
   raise it from about 113/s to 200/s without loosening the cap.
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
