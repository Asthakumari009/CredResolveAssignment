# Interview prep

Plain-English answers with exact file and function references. Paths are relative to the repo root.

## 1. Two workers reserve the same agent at the same time. Walk through it.

Both run `transitions.reserve_agent` inside their dispatch transaction
(`allocator/allocator.py: CallAllocator._create_call`):

```sql
UPDATE agents SET state = 'RESERVED', reserved_call_id = <my call>, ...
WHERE id = (SELECT id FROM agents WHERE campaign_id = $1 AND state = 'AVAILABLE'
            ORDER BY state_changed_at LIMIT 1 FOR UPDATE SKIP LOCKED)
  AND state = 'AVAILABLE'
RETURNING id
```

- Worker 1's subquery locks agent 7. Worker 2's subquery uses `SKIP LOCKED`, does not wait for agent 7, and
  takes agent 8 instead. No contention, no waiting.
- If `SKIP LOCKED` were removed, worker 2 would wait on agent 7's row lock. When worker 1 commits, Postgres
  re-evaluates `state = 'AVAILABLE'` on the new row version, which is now RESERVED. Worker 2 gets 0 rows and
  its whole call transaction rolls back, un-leasing the borrower. The worker keeps its claimed permit unit for its next call.
- Backstop: the partial unique index `one_live_call_per_agent` on `calls(agent_id)` for live
  states. Even a bug in the code above could not commit two live calls for one agent.
- Proof: `tests/test_races.py` runs 50 processes x 500 rounds against the real database, and the invariant
  "no agent ever bound to two calls at once" replays `agent_transitions`.

## 2. DB says AVAILABLE, cache says RESERVED. Which wins?

There is no cache (ADR-1). The database is the only place agent state lives, and every
decision that matters reads it under a row lock or inside a conditional UPDATE. If a cache were
added for dashboards, the database would always win. A cache could only ever be a hint to skip work, never
permission to do it. The reservation itself must be the atomic UPDATE above, because a cache read
followed by a write is exactly the check-then-act race the UPDATE closes.

## 3. Provider sends ANSWERED, worker crashes, then COMPLETED arrives.

`make demo-crash-after-answered` runs both variants with a real SIGKILL (`faults.crash_point`).

- The webhook receiver (`events/webhook_api.receive`) has already inserted ANSWERED into
  `provider_events`. Receiving does not depend on any worker.
- **Crash before COMMIT** (`event_answered_before_commit`): Postgres rolls the transaction back. The inbox
  row is still unprocessed, and another worker's `events/processor.process_one` claims it and applies it.
- **Crash after COMMIT** (`event_answered_after_commit`): ANSWERED is durable, and the agent is already
  CONNECTED. The next worker simply processes COMPLETED.
- COMPLETED may arrive while ANSWERED is still unprocessed. Per-call FIFO in `process_one` (ordered by
  `sequence, id`, with a 1500 ms hold for a missing sequence number) applies ANSWERED first. If ANSWERED were
  truly lost, `call_sm.ALLOWED` accepts COMPLETED from any live state, and the answer is inferred from
  `duration_s > 0` (`answered_inferred`).
- Both variants end in the same state: call COMPLETED, agent WRAP_UP then AVAILABLE, borrower DONE,
  and all invariants pass.

## 4. Predicted 70% answer rate, it drops to 10%. And the reverse.

The scenarios are E (20% to 70%) and F (70% to 10%) in `sim/scenarios.py`. `make demo-answer-rate-shock` prints every decision.

- **70% to 10% (safe direction):** pacing was dialing conservatively for 70%, so now fewer people answer than
  planned. Agents go idle and nobody is abandoned. `domain/safety_rules._fallback_reasons` sees
  ANSWER_RATE_DRIFT (short window far below the long window, z-test) and briefly forces progressive while
  the window relearns. Predictive then resumes at a much higher dial ratio. Measured: 0 abandons.
- **20% to 70% (dangerous direction):** pacing dialed for 20% and now 70% answer, so more people pick up than there
  are agents. Two defences, in order:
  1. HARD_RATIO_CAP (`safety_rules.decide`) caps unanswered in-flight calls at 2.5 x AVAILABLE agents now. As
     answered calls take agents, AVAILABLE shrinks and the cap tightens on the very next tick. Measured: it
     starts cutting 12 s after the jump.
  2. ANSWER_RATE_DRIFT forces progressive once the short window has enough resolved calls. Measured: +44 s.
     The lag comes from the window counting only calls whose outcome is known (dial-time sampling over
     the resolved prefix, `safety/snapshot.load_snapshot`).
  Result: 3 abandons out of 107 answered (2.8%) in the 5 minutes after the shock, under the 3% budget,
  after which ABANDON_BUDGET would have forced progressive anyway. Contained, not prevented.

## 5. 1,000 to 100,000 agents. What breaks first?

From `docs/SCALING.md`, measured:

- First: **the permit row**, measured and fixed. The old `consume_one` was the first statement in the dispatch
  transaction, so the campaign's permit row was locked through the whole transaction. Dispatch per campaign was serial,
  with a ceiling of about 110 to 120 reservations/s. Going from 16 to 32 workers *dropped* throughput from 123/s to 78/s, and
  every sampled lock wait was on that row.
- Real traffic is about 0.011 to 0.03 dials/s per agent, so 1,000 agents in one campaign is fine (about 30/s),
  10,000 in one campaign is not (about 300/s), and 100,000 needs partitioning everywhere.
- Fixed with batch claims (`SafetyController.claim_units` plus a `FOR KEY SHARE` pin per call). p50 reservation
  latency went from 18 to 4.6 ms, and throughput rose 28% to 77%. The new bottleneck is the pacing leader's tick cadence (it shares
  a loop with dispatch and dials, so it ticks every 340 to 570 ms instead of 250) and then the claim row itself.
- Next fixes: a dedicated pacing loop, shard the permit, partition by campaign or
  agent pool, PgBouncer, a partitioned inbox, sharded provider health.
- Adding servers alone makes it worse: more waiters on one lock, slower lock holders, and workers stuck
  on the lock are not processing events.

## 6. Why 17 calls instead of 10?

Every decision is a row in `safety_decisions`, with the proposal's explanation, the full snapshot,
the reason codes and the outcome. A real row from `make demo-agent-drop`:

```
id              61                 (make demo-agent-drop, before the drop)
worker_id       w1                 fencing_token 1
proposal_mode   PREDICTIVE         requested 17
outcome         REDUCE             approved_count 15     mode PREDICTIVE
reason_codes    {HARD_RATIO_CAP}
explanation     available=27, soon_free=0.5x14=7.0->7, waiting=0, ringing=52, p=0.29 (n=200),
                p_hi=0.36, C=34, target=1.0%. n=17 gives P(overflow)=0.93%, n=18 gives 1.22%.
                Proposing 17. (avg setup 0.6s, avg talk 80s)
                | cap = min(2.5 x 27 available, 500) - 52 in flight = 15
snapshot        staffed 100, available 27, wrap_up 14, connected 59, unbound_in_flight 52,
                window 59 answered / 200 resolved, abandon 0 / 100, breaker CLOSED, leader_ok true
```

Reading it in order:

1. **Why not 10, why 17?** The observed answer rate is 59/200 = 0.29, and its Wilson 95% upper bound is 0.36.
   Capacity C = 27 available + floor(0.5 x 14 in wrap-up) - 0 already waiting = 34. There are 52 calls
   ringing with no agent. With 52 + 17 = 69 calls at p = 0.36, P(more than 34 answer) = 0.93%, under
   the 1% target. 18 would make it 1.22%. So 17 is the largest safe number. 10 would leave agents idle for no
   safety gain.
2. **Why 15 dialed, not 17?** The Safety Controller's hard ratio cap: unanswered in-flight calls may not
   exceed 2.5 x agents AVAILABLE now. That is floor(2.5 x 27) = 67, minus 52 already in flight, which leaves 15.
   The statistics propose, and the deterministic rule decides.
3. Every input is in the snapshot the Safety Controller read itself, not the one pacing used.

How to read it: the explanation string is written by `pacing/predictive.propose` and names every term of
the formula. The reason codes and notes come from `domain/safety_rules.decide`. "Why N" is always
the predictive formula's N, reduced by whichever rule appears in `reason_codes`.

## 7. What part are you least confident about?

- **Answer-rate drift detection lag (about 45 s).** The answer-rate window only counts resolved calls. No-answers
  take 30 s to resolve, so a jump in answer rate shows up late. The hard ratio cap carries the risk until then. An earlier
  signal from answers alone, which resolve in seconds, is the fix I would build next.
- **The simulator and worker loops can drift** (ADR-6). They share every decision function but sequence them
  separately. Nothing automatically checks that the order of operations matches.
- **The reconcile give-up (TIMEOUT).** After `reconcile_give_up_s` of an unreachable provider, the call is marked
  FAILED even though the provider may have placed it. It is safe for agents and borrowers (late events are
  STALE_IGNORED), but the attempt count can be wrong by one.
- **Leader takeover.** For one tick around a takeover, the new leader's snapshot may not include the old
  leader's last consumed units. The hard cap bounds the overcount, but I have not measured it under load.

## 8. What would you do with one more week?

1. **Fix the measured next bottleneck: pacing tick cadence (NOT BUILT).** After batch claims, the leader
   ticks every 340 to 570 ms instead of 250 ms. It is an ordinary worker, so dials (up to 3 s) and event
   batches sit in front of its tick, and agents who free up wait for the next permit (`docs/SCALING.md`,
   "After: what is the bottleneck now"). The fix is a dedicated pacing loop: a thread or process that only runs
   `try_lead`, pacing and `SafetyController.decide`. Then shard the permit (SCALING fix 3) and rerun the
   8/16/32 sweep. Not needed at 1,000 agents (about 30 dials/s against about 160/s measured), so it
   was left out in the feature freeze.
2. Early drift signal from answers only, and attribute abandons per provider so Provider B's event
   loss does not count against pacing.
3. A property-based test that generates random event orders, duplicates and drops per call and checks
   the final state against the provider's truth.
4. Run the simulator against the worker's orchestration by giving it a repository interface (ADR-6's
   declined option), or at least a shared step function, to close the drift risk.
5. A real provider adapter with webhook signature verification.

## 9. Five more hard questions

**Q: Why not SERIALIZABLE isolation?** Every race is on a known row and closed by one statement
(conditional UPDATE, SKIP LOCKED, unique index), each marked `# CONCURRENCY:`. SERIALIZABLE would add
retry loops everywhere and hide which statement does the protecting (ADR-10). The cost is that lock order
must be kept by hand. The load test found one place where a foreign key's implicit lock broke it (PLAN D10).

**Q: How do you prevent dialing the same borrower twice?** `allocator.lease_borrower` moves the borrower
PENDING to IN_PROGRESS with `SKIP LOCKED` and a state re-check, in the same transaction that inserts the call.
Backstop: `one_live_call_per_borrower`, a partial unique index on live calls. Retries are only scheduled in
`transitions._settle_borrower` after the call is terminal, and an answered call is never retried.

**Q: A worker is paused for 30 s (not dead), then wakes up mid-dispatch. What happens?** Every write it
makes is fenced. Its claimed permit units can only be spent through `SafetyController.pin`, which
checks the permit is unrevoked, unexpired and carries the current `fencing_token`. If its lease expired,
the new leader bumped the token, and its units are dead. Its agent
transitions carry `reserved_call_id` (`transitions.set_agent_state`). If the agent was released and
re-reserved, those change 0 rows. Residual risk: if it dials after the reaper already got NOT_FOUND and
cancelled the call, `record_dial_result` sees CANCELLED and hangs up immediately. The borrower may hear
one ring that is cut off.

**Q: How do you know the invariants actually hold, rather than the tests passing by luck?**
`smartdialer/invariants.check_all` replays the history tables (`call_transitions`, `agent_transitions`,
`calls`, `dial_permits`). It checks overlap of borrower calls by time, agent binding chains, legal
chained call transitions, no duplicates, permits never over-claimed, and calls per permit never more
than claimed units. It runs after every demo, after the 50-process race tests, and after each load run with
6,583 / 7,565 / 5,057 calls. Several of them are also database constraints, so a violation could not commit.

**Q: Why is the abandon budget 3% with hysteresis at 1.5% and 60 s?** 3% is the common regulatory
ceiling for abandoned calls (for example the US TCPA safe harbor and UK Ofcom). Without hysteresis the
rate oscillates around 3%: trip, progressive, the rate drops, resume, overdial again. Requiring the rate
to stay below 1.5% for 60 s before resuming stops that flapping (`safety_rules._fallback_reasons`).
It needs at least 50 answered calls, so one abandon out of three answers does not trip it.
