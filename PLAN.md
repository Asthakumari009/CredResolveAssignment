# SmartDialer: Phase 0 Plan

Status: **approved with conditions (below)**. Phase 1 has not started; waiting on answers to section 12.

Sources: `ASSIGNMENT.md` (the CredResolve assignment) and the build brief. Where this plan departs from the brief, the change is listed in section 11 with a reason, and the decisions I need from you are in section 12.

## Approval conditions (binding, they override anything below that conflicts)

| # | Condition | Where it lands |
| --- | --- | --- |
| C1 | Repository change accepted. ADR must state the tradeoff: the orchestration loop exists in both the simulator and the worker and could drift. Keep both loops as thin as possible. | `docs/ADR.md`; `worker/main.py` and `sim/simulator.py` only sequence calls to shared pure functions |
| C2 | `reserved_call_id` fences EVERY agent transition after reservation (DIALING, CONNECTED, WRAP_UP, pause handling during setup, release), not only release. Test a stale worker attempting each one. | section 4; `tests/test_agent_fencing.py` |
| C3 | One fixed lock order, documented and used everywhere. Tests for both crash windows (after commit before dial; after provider accepted before INITIATED recorded), both resolved via `get_call_status` by idempotency key. | section 5.0; `tests/test_crash_windows.py` |
| C4 | Permit consume checks `revoked = false`. Test that a revoked permit cannot be consumed. | section 5.2; `tests/test_permits.py` |
| C5 | Note in `SCALING.md` that the single `provider_health` row is a write hotspot, with the fix. Keep the simple version. | `docs/SCALING.md` |
| C6 | `UNIQUE (call_id, to_state)` only on `call_transitions`; insert with `ON CONFLICT DO NOTHING`, treat as DUPLICATE so the transaction does not abort. | section 4 |
| C7 | Build order: state machines, race-safe reservation, inbox and event processing, crash recovery, Safety Controller, failure demos. Simulation and load test after. | section 13 |
| C8 | All Python runs inside Docker (an `app` service in compose). Makefile and `tasks.py` are thin wrappers over `docker compose run`. Crash demos use `docker kill` or in-container SIGKILL. | sections 2, 9 |
| C9 | `git init` now, commit `PLAN.md` and `ASSIGNMENT.md`. | done |

---

## 1. The core idea in one paragraph

PostgreSQL is the only source of truth and the only coordination mechanism. Every place where two workers can race is settled by one atomic statement in the database (a conditional `UPDATE ... RETURNING`, `SELECT ... FOR UPDATE SKIP LOCKED`, or a unique index), never by memory. All decision logic (state machines, pacing math, safety rules, circuit breaker) is pure functions that take a snapshot and return a decision, so they can be unit tested, reused unchanged by the simulator, and explained line by line. The pacing engine can only *propose*; it physically cannot dial, because dialing requires consuming a row in `dial_permits`, and only the Safety Controller creates those rows.

---

## 2. Modules

```
smartdialer/
  config.py          # dataclasses with every tunable (defaults from the brief)
  db.py              # psycopg 3 connection pool + tx helper
  domain/            # PURE, no I/O
    agent_sm.py      # agent states, allowed-transitions table, apply()
    call_sm.py       # call states, ranks, allowed-transitions table, apply_event()
    safety_rules.py  # decide(proposal, snapshot, config, prev_mode) -> SafetyDecision
    stats.py         # Wilson bound, binomial tail, sliding windows
    breaker.py       # circuit breaker transition function
    backoff.py       # exponential backoff with full jitter
    calling_window.py
  pacing/            # PURE, proposal only. May import domain/, nothing else.
    progressive.py   # propose(snapshot) -> PacingProposal
    predictive.py    # propose(snapshot, config) -> PacingProposal (with explanation)
  safety/
    snapshot.py      # reads its OWN snapshot from the DB
    controller.py    # SafetyController: snapshot -> safety_rules.decide -> log -> permit
  allocator/
    allocator.py     # CallAllocator(safety_controller, provider_client): consume permit, lease, dispatch
  events/
    webhook_api.py   # FastAPI: POST /events/{provider} -> insert into inbox, always 200
    processor.py     # claim inbox rows SKIP LOCKED, apply call_sm, write results
    answer_hold.py   # predictive: claim agent for ANSWERED calls, abandon after max hold
  recovery/
    heartbeat.py
    reaper.py
  providers/
    interface.py     # Provider protocol: dial, hangup, get_call_status
    client.py        # HTTP client for the mock providers, timeouts
  worker/
    main.py          # loop: heartbeat, lead/pace, dispatch, process events, hold, reap
  faults.py          # named crash points, no-op unless an env var enables one
mock_provider/
  app.py             # FastAPI, one process, serves both A and B under /a and /b
  behaviours.py      # latency, errors, duplicates, reordering, drops, outage toggle
sim/                 # simulator.py, scenarios.py, charts.py
loadtest/            # run.py, report.py
demos/               # one script per failure scenario + shared timeline printer
tests/
docs/
migrations/          # plain numbered .sql files, applied by a 30-line runner
docker-compose.yml   # postgres:16 + app (python 3.12 image; workers, webhook, mock provider, tests) (C8)
Dockerfile           # app image
Makefile             # thin wrapper: each target is one `docker compose run --rm app ...`
tasks.py             # same targets for machines without make; also only shells out to docker compose
pyproject.toml
```

Dependency rule (enforced by `tests/test_architecture.py`, which parses imports with `ast`): `pacing/` imports only `domain/` and stdlib. `domain/` imports only stdlib. Nothing imports `allocator/` except `worker/`.

---

## 3. Tables

All timestamps are `timestamptz` and use the DB clock (`now()`), never worker clocks, so lease expiry has one clock.

| Table | Key columns | Notes |
| --- | --- | --- |
| `campaigns` | id, mode, settings jsonb, timezone (default `Asia/Kolkata`), pacing_lease_owner, pacing_lease_expires_at, fencing_token bigint, safety_mode, safety_mode_since | `safety_mode` holds hysteresis state (PREDICTIVE / FORCED_PROGRESSIVE / COOLDOWN) |
| `workers` | id, started_at, last_heartbeat_at | |
| `agents` | id, campaign_id, state, reserved_call_id, reserved_by_worker, lease_expires_at, last_heartbeat_at, state_changed_at | `last_heartbeat_at` is the human agent's client presence |
| `agent_transitions` | id, agent_id, from_state, to_state, call_id, worker_id, reason, at | append only |
| `borrowers` | id, campaign_id, phone, state (PENDING / IN_PROGRESS / DONE / EXHAUSTED), attempts, next_attempt_at, locked_by_worker, lease_expires_at, last_outcome | |
| `calls` | id uuid (generated by us, = provider idempotency key), campaign_id, borrower_id, agent_id null, mode, state, state_rank, provider_name, provider_call_id, failure_reason, owner_worker, lease_expires_at, permit_id, created_at, dialed_at, answered_at, connected_at, ended_at, duration_s, answered_inferred bool | one row per dial attempt, so it is also the attempt log for provider health |
| `call_transitions` | id, call_id, from_state, to_state, cause_event_id, worker_id, at | **UNIQUE (call_id, to_state)** |
| `provider_events` | id, provider_name, provider_event_id, provider_call_id, call_id, event_type, sequence, provider_timestamp, payload jsonb, received_at, attempts, processed_at, processing_result | **UNIQUE (provider_name, provider_event_id)**; partial index on unprocessed rows |
| `dial_permits` | id, campaign_id, fencing_token, mode, approved_count, consumed_count, expires_at, revoked, decision_id | CHECK `consumed_count <= approved_count` |
| `safety_decisions` | id, campaign_id, at, worker_id, fencing_token, proposal jsonb (requested + explanation), snapshot jsonb, approved_count, outcome (APPROVE / REDUCE / REJECT / FALLBACK_TO_PROGRESSIVE), mode, reason_codes text[] | answers "why 17 and not 10" |
| `provider_health` | provider_name, breaker_state, opened_at, half_open_probes, updated_at | one row per provider |

Backstop constraints (the last line of defence; the code should never hit them, and a hit is logged as a bug):

- `CREATE UNIQUE INDEX one_live_call_per_borrower ON calls(borrower_id) WHERE state NOT IN (terminal states)`
- `CREATE UNIQUE INDEX one_live_call_per_agent ON calls(agent_id) WHERE agent_id IS NOT NULL AND state NOT IN (terminal states)`
- `UNIQUE (call_id, to_state)` on `call_transitions`: since ranks only move forward, a call can enter each state at most once. A duplicate transition is not just "not done", it is impossible to write.
- `CHECK (consumed_count <= approved_count)` on permits.
- One active permit per campaign: `CREATE UNIQUE INDEX ON dial_permits(campaign_id) WHERE NOT revoked`. Issuing a new permit revokes the previous one in the same transaction, so stale unconsumed permits cannot stack up.

Isolation level: READ COMMITTED everywhere, with explicit row locks. I am not using SERIALIZABLE because every race here is on a specific row and is closed by a conditional UPDATE or a row lock; SERIALIZABLE would add retry loops everywhere for no extra safety. This is in the ADR.

---

## 4. State machines (pure, table driven)

### Agent

| From | To | Trigger |
| --- | --- | --- |
| OFFLINE | AVAILABLE | agent logs in |
| AVAILABLE | RESERVED | progressive allocator wins the atomic reserve |
| AVAILABLE | CONNECTED | predictive: claimed for an already ANSWERED call |
| RESERVED | DIALING | provider accepted the dial (call INITIATED) |
| RESERVED / DIALING | AVAILABLE | call failed / cancelled / reservation lease expired |
| DIALING | CONNECTED | borrower answered |
| CONNECTED | WRAP_UP | call ended |
| WRAP_UP | AVAILABLE | agent finishes notes (sim: timer) |
| AVAILABLE / WRAP_UP / RESERVED / DIALING | PAUSED or OFFLINE | agent action or presence timeout |
| PAUSED | AVAILABLE | agent resumes |
| CONNECTED | OFFLINE | agent drops mid-call (call continues to completion; recorded) |

The reserve is:
```sql
-- CONCURRENCY: two workers can both read this agent as AVAILABLE. Only one UPDATE can
-- match state='AVAILABLE' because Postgres takes the row lock for the UPDATE and the
-- second UPDATE re-checks the WHERE clause after the first commits. Loser gets 0 rows.
UPDATE agents SET state='RESERVED', reserved_call_id=%s, reserved_by_worker=%s, lease_expires_at=now()+%s
WHERE id = (SELECT id FROM agents WHERE campaign_id=%s AND state='AVAILABLE'
            ORDER BY state_changed_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id;
```
**Fencing (C2).** Once an agent is reserved, `reserved_call_id` is its fencing token. Every later agent transition driven by a worker carries it, together with the expected current state:
```sql
-- CONCURRENCY: a stale worker (crashed and restarted, or slow) may still hold an old call id.
-- If the agent has since been released and re-reserved for a different call, reserved_call_id
-- no longer matches and this UPDATE changes 0 rows. The stale worker treats 0 rows as "not mine".
UPDATE agents SET state=%(to)s, ...
WHERE id=%(agent)s AND reserved_call_id=%(call)s AND state=%(expected_from)s
```
This covers RESERVED -> DIALING, DIALING -> CONNECTED, CONNECTED -> WRAP_UP, the cancel/hand-off path when an agent pauses during setup, and release to AVAILABLE. The only worker-driven agent transitions without it are the two acquisitions (AVAILABLE -> RESERVED, AVAILABLE -> CONNECTED), which are guarded by `state='AVAILABLE'` and set `reserved_call_id`. Agent-driven actions (log in, pause, resume, finish wrap-up) come from the agent themselves, not a worker; finishing wrap-up clears `reserved_call_id`. `tests/test_agent_fencing.py` has a stale worker attempt each fenced transition after the agent was re-reserved, and asserts 0 rows and no `agent_transitions` row. This is why agents need no `version` column: the ABA problem is solved by the call id, which is never reused.

"Which agent": the one AVAILABLE longest (`ORDER BY state_changed_at`), which is fair and spreads load.

### Call

Ranks: QUEUED 0, RESERVED 1, INITIATED 2, RINGING 3, ANSWERED 4, CONNECTED 5, terminal 9: COMPLETED, FAILED(NO_ANSWER / BUSY / TIMEOUT / PROVIDER_ERROR), CANCELLED, ABANDONED.

Rules in `call_sm.apply_event(call, event) -> Outcome(new_state | STALE_IGNORED | DUPLICATE | REJECTED)`:
- Only forward moves in rank. Same state again = DUPLICATE. Lower rank = STALE_IGNORED.
- Terminal is absorbing: any event on a terminal call = STALE_IGNORED.
- COMPLETED without ANSWERED: accepted; if `duration > 0` set `answered_inferred = true` for stats.
- Predictive: ANSWERED does not mean CONNECTED. CONNECTED happens only when we bridge an agent. A call that was answered (real or inferred) and never CONNECTED counts as an abandon in all stats, even if the borrower hung up first.
- Progressive: the agent is already bound, so ANSWERED moves the call to CONNECTED in the same transaction.

Event application, one transaction:
```
BEGIN
  SELECT ... FROM provider_events WHERE processed_at IS NULL ORDER BY received_at
    LIMIT n FOR UPDATE SKIP LOCKED          -- CONCURRENCY: each inbox row to exactly one worker
  SELECT ... FROM calls WHERE provider_call_id=? FOR UPDATE   -- serialize all events for one call
  outcome = call_sm.apply_event(...)        -- pure
  UPDATE borrowers, then agents              -- side effects, in the global lock order (5.0)
  INSERT call_transitions ... ON CONFLICT (call_id, to_state) DO NOTHING
                                             -- 0 rows inserted: outcome becomes DUPLICATE, side
                                             -- effects above are skipped, tx does not abort (C6)
  UPDATE provider_events SET processed_at=now(), processing_result=...
COMMIT
```
Why a pessimistic row lock instead of an optimistic version check: events for one call are few and must be applied strictly one at a time; an optimistic check would only turn the same serialization into retries. The lock is held for milliseconds and never across network I/O.

Duplicates are blocked twice: the inbox unique constraint drops exact redeliveries at insert, and the state machine rejects ANSWERED -> ANSWERED if the provider sends a new event id for the same fact. The third backstop is the unique index on transitions.

Orphans (event for an unknown `provider_call_id`, e.g. it arrives before the dial response was saved): left unprocessed with `attempts += 1`, retried for up to 30 s, then marked ORPHAN. The webhook also carries our call UUID (we pass it to the provider as the idempotency key), so most "orphans" can be matched by `call_id` directly.

Both diagrams go in `docs/STATE_MACHINES.md` as Mermaid.

---

## 5. Flows

### 5.0 Global lock order (C3)

Every transaction that locks more than one of these rows takes them in this order, and only in this order:

**campaign / permit -> call -> borrower -> agent**

- Dispatch: permit, borrower, agent, then inserts the call (a new row nobody else can be waiting on; the borrower is already locked, so the borrower partial unique index cannot make two inserts wait on each other).
- Event processor, answer_hold, reaper: call, then borrower, then agent.
- Agent-driven actions (pause, go offline) lock only the agent row; the resulting call cleanup is done in a separate transaction by a worker in the normal order.

Why it matters: two transactions taking the same two rows in opposite orders can deadlock. Postgres would detect it and abort one, so it is not a correctness bug, but it turns into retries under load. With one order, it cannot happen. The order is written in a module docstring in `db.py` and each multi-row transaction has a `# LOCK ORDER:` comment.

Rule within the agent step: to make "side effects in order" possible, side-effect code first decides everything purely from the locked call row, then issues the borrower UPDATE, then the agent UPDATE.

### 5.1 Pacing tick (one leader per campaign)

1. Acquire or renew the campaign pacing lease:
   ```sql
   -- CONCURRENCY: only one worker can hold an unexpired lease. A takeover increments the
   -- fencing token, which instantly invalidates every permit issued by the previous leader.
   UPDATE campaigns SET pacing_lease_owner=me, pacing_lease_expires_at=now()+'3s',
     fencing_token = CASE WHEN pacing_lease_owner=me THEN fencing_token ELSE fencing_token+1 END
   WHERE id=? AND (pacing_lease_owner=me OR pacing_lease_expires_at < now())
   RETURNING fencing_token;
   ```
2. Pacing engine builds a `PacingProposal(requested_calls, explanation)` from its snapshot.
3. Safety Controller reads its **own** snapshot (one query, same transaction as the permit insert), runs `safety_rules.decide(...)`, writes a `safety_decisions` row, revokes the previous permit, inserts a new permit with `approved_count`, `expires_at = now()+2s`, and the fencing token. The insert is conditional on the token still matching (`INSERT ... SELECT ... WHERE campaigns.fencing_token = %s`).

Why this stops N workers from each overdialing: only the leader can create permits, there is at most one live permit per campaign, and any worker consuming it re-checks the token. So the total number of dials per tick is bounded by one `approved_count`, no matter how many workers dispatch.

### 5.2 Dispatch (any worker)

One transaction per call slot, then the network call outside any transaction:
```
BEGIN
  UPDATE dial_permits SET consumed_count=consumed_count+1
    WHERE id=? AND NOT revoked AND consumed_count < approved_count AND expires_at > now()
      AND fencing_token = (SELECT fencing_token FROM campaigns WHERE id=?)
    RETURNING id                              -- 0 rows: stop dispatching
  lease one borrower (FOR UPDATE SKIP LOCKED, PENDING, next_attempt_at <= now(),
                      attempts < max, inside calling window in campaign timezone)
  progressive only: reserve agent (section 4)
  INSERT calls (id = new uuid, state RESERVED (progressive) or QUEUED (predictive), owner_worker=me, lease)
COMMIT                                        -- any step returns nothing: ROLLBACK, permit unit is returned
provider.dial(idempotency_key=call.id, phone)
UPDATE calls SET state='INITIATED', provider_call_id=?
  WHERE id=? AND state IN ('RESERVED','QUEUED') AND owner_worker=me   -- 0 rows: reaper took it, hang up
```
Why the order is "permit, borrower, agent, call row" in one transaction: it is all-or-nothing, so there is no partial state to clean up if any step fails, and the agent becomes RESERVED only at the instant a call row exists that it is reserved for. The agent's reservation is visible in the DB before the provider is ever contacted, which is what makes a crash between commit and dial recoverable.

Why the provider call is outside the transaction: holding row locks across a 2 s network call to Provider B would serialize the whole campaign.

The permit consume checks `NOT revoked` in its WHERE clause (C4); `tests/test_permits.py` covers revoked, expired, exhausted, and wrong-token permits, each returning 0 rows.

**The two crash windows (C3), both resolved the same way:**

| Window | DB state left behind | Provider state | Reaper action |
| --- | --- | --- | --- |
| W1: after COMMIT, before `dial` | call RESERVED/QUEUED, agent RESERVED, borrower IN_PROGRESS | no call for this key | `get_call_status(call.id)` -> NOT_FOUND -> call CANCELLED, agent released (fenced), borrower PENDING without counting an attempt |
| W2: provider accepted `dial`, before INITIATED is written | same as W1 | live call for this key | `get_call_status(call.id)` -> live -> adopt: write INITIATED + provider_call_id, set owner to the reaper's worker, keep the agent, continue from events already in the inbox |

Both are tested in `tests/test_crash_windows.py` by crashing a worker at the named fault point (in-container SIGKILL), then running the reaper and asserting the end state and invariants. Known residual window, documented honestly: if the "dead" worker was only paused (not dead) and dials after the reaper got NOT_FOUND, its conditional INITIATED update finds the call CANCELLED and it immediately hangs up. The borrower may see one ring that is cut. Fix for a real provider: a provider-side cancel-by-key tombstone.

Call failure (NO_ANSWER, BUSY, TIMEOUT, PROVIDER_ERROR): agent back to AVAILABLE (guarded by reserved_call_id), borrower back to PENDING with `next_attempt_at = now() + full_jitter(base * 2^attempts)`, or EXHAUSTED at max attempts. A call that was ANSWERED (real or inferred) is never retried; the borrower becomes DONE with `last_outcome`.

Agent disappears during setup (goes PAUSED / OFFLINE while RESERVED or DIALING):
- call not answered yet: hang up at the provider, call CANCELLED, borrower back to PENDING without counting an attempt.
- call already answered: try to claim another AVAILABLE agent atomically; if none within `max_answer_hold_ms`, ABANDONED with the simulated polite message, counted.

### 5.3 Predictive answer handling

On ANSWERED for an agentless call, the processor tries to claim an AVAILABLE agent in the same transaction (atomic UPDATE as in section 4, AVAILABLE -> CONNECTED). If none is free, the call stays ANSWERED. `answer_hold.py` runs every 100 ms: claim agents for waiting ANSWERED calls (oldest first), and mark ABANDONED any that waited longer than `max_answer_hold_ms` (default 2000), using `answered_at` from the DB clock.

### 5.4 Reaper (any worker, every 1 s, each item claimed with SKIP LOCKED)

A worker is dead when `last_heartbeat_at < now() - 5s`. Leases are 10 s and renewed by the owning worker's heartbeat.

| Found | Action |
| --- | --- |
| Agent RESERVED, lease expired, its call never left RESERVED | release to AVAILABLE |
| Call RESERVED / QUEUED / INITIATED, owner dead | `get_call_status(call.id)`. Not found: CANCELLED, release agent and borrower. Found and live: adopt (set owner to me) and continue. Found and ended: apply as an event. **Never redial blindly.** |
| Call ANSWERED, owner dead | progressive: connect to its reserved agent; predictive: handed to answer_hold, which claims or abandons |
| Borrower IN_PROGRESS, lease expired, no live call | back to PENDING |
| Agent with no presence heartbeat for 30 s | OFFLINE (same path as "agent disappears") |
| Inbox rows | nothing to do: an unfinished transaction from a dead worker rolled back and the rows are unlocked |

---

## 6. Predictive pacing (`pacing/predictive.py`)

Inputs from the snapshot: available agents A, agents in WRAP_UP with expected remaining wrap time below average setup time (W), calls dialing or ringing R, calls ANSWERED and waiting for an agent H, answered/attempted counts in a sliding window (last 200 resolved dials), average setup time, average talk time.

- `p` = answered / resolved in the window, `p_hi` = Wilson upper bound at 95%. High answer rate is the dangerous direction, so we plan for `p_hi`.
- Capacity `C = A + floor(0.5 * W) - H`. Soon-free agents are discounted by half and rounded down.
- Choose the largest `n >= 0` with `P(Binomial(R + n, p_hi) > C) <= target_overflow` (default 1%). Exact binomial tail computed with `math.comb` in log space; `R + n` is at most a few hundred so this is cheap.
- The explanation string includes every input and the overflow probability at `n` and `n + 1`, e.g. `available=10, soon_free=1.4->1, ringing=3, waiting=0, p=0.31 (n=412), p_hi=0.35, C=11, target=1%. n=17 gives P(overflow)=0.9%, n=18 gives 1.2%. Proposing 17.`

Simplification to call out: this treats each in-flight call as an independent Bernoulli trial and ignores *when* answers arrive relative to agents freeing up. That makes it conservative (it assumes all answers land before any agent frees). It is in the "least confident" section.

Progressive proposal: `requested = A` (agent reservation is the real bound anyway).

---

## 7. Safety Controller (`domain/safety_rules.py`, pure; `safety/controller.py`, I/O)

`decide(proposal, snapshot, cfg, prev_mode, now) -> SafetyDecision(approved_calls, mode, outcome, reason_codes)`. Rules are applied in order; the strictest result wins; every rule that fired is listed.

| # | Code | Rule | Effect |
| --- | --- | --- | --- |
| 8 | NOT_PACING_LEADER | token in snapshot != campaign token | REJECT |
| 7 | STALE_SNAPSHOT | snapshot older than 1 s | REJECT |
| 6 | PROVIDER_HEALTH | breaker OPEN / HALF_OPEN or DEGRADED | REJECT / reduce to probe count or 50% |
| 1 | COLD_START | fewer than 50 resolved dials in window | FALLBACK_TO_PROGRESSIVE |
| 3 | ABANDON_BUDGET | abandon rate over last 100 answered >= 3% | FALLBACK, stay until rate < 1.5% for 60 s (hysteresis) |
| 4 | AGENT_DROP | available fell > 20% vs max in last 10 s (from `safety_decisions` history) | FALLBACK for 30 s cooldown |
| 5 | ANSWER_RATE_DRIFT | short window (last 30) differs from long window (last 200) by > 0.15 absolute | FALLBACK until stable for 30 s |
| 2 | HARD_RATIO_CAP | unanswered in flight + n > 2.5 x A, or > absolute cap (500) | REDUCE to the cap |

FALLBACK_TO_PROGRESSIVE means `approved = A - R_unbound` style progressive budget and the dispatcher reserves agents up front, so the progressive guarantee holds during fallback. The proposal cannot set mode, cannot bypass rules, and the allocator has no code path without a permit: the `CallAllocator` constructor requires a `SafetyController`, and the only function that inserts a call row takes a consumed permit id.

Answer-rate shock behaviour (to be shown in the demo):
- 20% -> 70% (dangerous): `p_hi` lags, so ANSWER_RATE_DRIFT fires within about 30 resolved calls, and HARD_RATIO_CAP bounds the damage before that. Abandons in that window are measured and reported, not assumed zero.
- 70% -> 10% (safe direction): no compliance risk, agents go idle; drift rule still falls back briefly, then the window relearns and predictive resumes.

Circuit breaker (`domain/breaker.py`): pure function of (state, recent dial results from `calls`, now). CLOSED -> OPEN when timeout+error rate over the last 20 dials in 30 s >= 50%; OPEN -> HALF_OPEN after 10 s; HALF_OPEN allows 3 probe dials; all succeed -> CLOSED, any fail -> OPEN. The pacing leader evaluates it and writes `provider_health`; every worker and the Safety Controller read that row, so there is one shared view.

---

## 8. Mock providers (`mock_provider/`)

One FastAPI process, two personalities behind the same HTTP API (`POST /{p}/dial`, `POST /{p}/hangup`, `GET /{p}/calls/{key}`, `POST /{p}/control`). `dial` is idempotent on our key: the same key returns the same provider call id. The outcome of each call (answer or not, talk time) is drawn from scenario parameters at dial time, and events are delivered by POST to the webhook with retries.

- A: 50 to 150 ms, 1% errors, in order, no duplicates.
- B: 300 to 2000 ms, 8% timeouts (the request hangs past the client timeout, but the call *may* still have been placed, which is exactly why reconciliation is by idempotency key), 15% duplicate events, 20% reordered, 5% missing intermediate events (RINGING or ANSWERED dropped).
- `/control` sets answer rate, talk time distribution, and outage mode at runtime.

---

## 9. Failure scenarios: how each is handled and shown

All demos run inside the `app` container against real Postgres and real worker processes, print a timeline, and finish with the invariant checker (section 10). Crash points use `faults.py`: when `SMARTDIALER_CRASH_AT=<point>` is set, the worker sends itself SIGKILL (`os.kill(os.getpid(), signal.SIGKILL)`) at that point. That is a real abrupt death with no cleanup handlers (Postgres sees the connection drop and rolls back any open transaction). Where exact timing does not matter, the demo kills a worker process from outside with SIGKILL, or kills a worker container with `docker kill` (C8).

| Demo | Handling |
| --- | --- |
| worker-crash | Crash after the provider accepted the dial. Reaper sees dead owner, calls `get_call_status(call_id)`, adopts the live call, events continue. Second variant: crash before dial, provider says not found, call CANCELLED, agent and borrower released, borrower redialed later with a **new** call id. Checks: no borrower with two live calls, no agent stuck in RESERVED. |
| provider-outage | Outage toggled on B. In-flight calls get no events; reaper and a status poller reconcile them by key. New dials time out, breaker opens, Safety Controller rejects with PROVIDER_HEALTH, borrowers back off with jitter. Outage off: HALF_OPEN probes, CLOSED, pacing resumes from progressive. |
| agent-drop | 100 available, 40 set OFFLINE over 3 s. Prints time from first drop to first decision with AGENT_DROP (bounded by one pacing tick, about 250 ms, plus permit expiry of 2 s worst case) and abandons during the window. |
| duplicate-events | Same ANSWERED posted 5 times (same event id, then new ids). Shows 1 APPLIED, 4 DUPLICATE rows in the inbox and exactly one ANSWERED row in `call_transitions`. |
| out-of-order | Post COMPLETED, ANSWERED, RINGING. Final state COMPLETED, the other two STALE_IGNORED, answered inferred from duration. |
| crash-after-answered | Variant 1: crash inside the transaction applying ANSWERED: rolled back, another worker applies it. Variant 2: crash after commit, before agent bridging: answer_hold on another worker claims an agent or abandons. Then COMPLETED arrives and is applied normally. |
| answer-rate-shock | Run in simulator and live: 20% -> 70% and 70% -> 10%, printing safety decisions and reason codes around the change. |

---

## 10. Invariant checker (`smartdialer/invariants.py`)

Pure SQL queries over the history tables, used by every demo, the load test, and tests:
1. No agent ever had two overlapping non-terminal calls (interval overlap on `call_transitions`).
2. No borrower ever had two overlapping non-terminal calls.
3. No duplicate `(call_id, to_state)` and every transition is in the allowed table.
4. No permit with `consumed_count > approved_count`; number of calls per permit equals `consumed_count`.
5. Progressive: agent-bound in-flight calls never exceeded available-plus-bound agents at any time.
6. No agent in RESERVED with an expired lease after the reaper has run.

---

## 11. Changes from the brief (please check)

1. **Simulator does not use a `Repository` interface with two implementations.** Instead, every decision (state machines, pacing, safety rules, breaker, backoff) is a pure function that the simulator and the live system both call. The simulator keeps its own in-memory world and a virtual clock. The Postgres layer is tested by the concurrency tests, demos, and load test. Reason: a second repository plus a contract suite roughly doubles the persistence code, and the thing worth sharing (the decisions) is already shared. The cost: the simulator does not exercise SQL. See Q1.
2. **No `version` columns** on agents or calls. Agents are guarded by `reserved_call_id` (prevents ABA), calls by the row lock. A version column would be unused and I would have to explain why it is there.
3. **Permit is consumed in the same transaction as borrower lease, agent reserve and call insert,** rather than after the call row exists. All-or-nothing, nothing to unwind.
4. **One active permit per campaign** (new permit revokes the old one), so unconsumed permits cannot add up across ticks.
5. **Provider health lives in the DB** (`provider_health` table, computed from `calls`), not in each worker's memory, so all workers and the Safety Controller agree. Only the pacing leader writes it. `SCALING.md` will note the single row per provider is a write hotspot at scale and give the fix (C5): per-worker or per-campaign health rows aggregated on read, or health computed on read from a time-bucketed counter table.
6. **`call_transitions` has UNIQUE (call_id, to_state)**, which makes "no duplicate transitions" a constraint rather than only a test. Inserts use `ON CONFLICT DO NOTHING`; a conflict means DUPLICATE (C6). `agent_transitions` has no such constraint because agents legitimately repeat states.
7. **Added `answer_hold` loop and `faults.py`**; neither is in the brief's layout, both are needed for 6.4 and the crash demos.
8. Dropped the separate `domain/pacing.py`: pacing math lives in `pacing/predictive.py` (still pure).

---

## 12. Decisions I need from you

**Q1. Simulator design.** (a) my recommendation in 11.1: shared pure functions, no repository abstraction; or (b) the brief as written: `Repository` interface, Postgres + in-memory implementations, contract suite on both.

**Q2. Scope vs the 4 to 6 hour timebox.** The brief is several days of work, against a stated 4 to 6 hour budget. Reviewers may read a very large submission as "did not respect the timebox" or may not read it at all. I suggest building in this priority order so the repo is coherent if you stop at any phase: (1) schema, state machines, reservation races, progressive dialer; (2) inbox, event processor, mock providers, duplicate/out-of-order; (3) reaper and crash demos; (4) predictive, Safety Controller, permits; (5) simulation; (6) load test and scaling doc; (7) remaining docs. Same phases as the brief, just confirming you want all of them. The extra-hard parts I would trim first if needed: the 2x / 4x load test reruns and the property-based test.

Note: Q3 and Q4 are settled by C8 (all Python in Docker, Makefile and `tasks.py` as thin `docker compose run` wrappers), and Q5 by C9. They are kept below for the record.

**Q3. Windows tooling.** You are on Windows. `make` is not installed by default. Options: (a) Makefile plus a `tasks.py` (`uv run python tasks.py test`) that does the same things, so it works with or without make; (b) Makefile only, and you install make (e.g. `winget install GnuWin32.Make` or use Git Bash). I recommend (a). Also: Docker Desktop must be running for `docker compose up`.

**Q4. What runs in Docker.** (a) only Postgres in compose; workers, webhook receiver and mock provider run locally via uv and are started by the demo / sim / loadtest scripts (simpler, faster to debug); or (b) everything in compose. I recommend (a), with a note in the README.

**Q5. Git.** `C:\smartdialer` is not a git repository. OK to `git init` and make the Phase 0 commit (`ASSIGNMENT.md`, `PLAN.md`) after you approve? Is there a remote to push to, or local only?

**Q6. Real provider.** The assignment mentions Plivo as a bonus. I plan to skip it and say so in the ADR (it adds credentials and real phone calls to a prototype whose point is the control plane). Agree?

---

## 13. Phases (each ends with the full test suite green, a commit, and a 5-line summary)

Order follows C7: correctness first, simulation and load test last.

0. This plan.
1. Docker setup (postgres + app), schema, migrations, pure state machines, unit tests for every allowed and disallowed pair.
2. Agent reservation with full `reserved_call_id` fencing, borrower leasing, permits (basic, revoked check), progressive dialer, 50-process race tests (agent, borrower, permit), stale-worker fencing test, progressive 50-agent invariant test.
3. Provider interface, mock providers A and B, webhook receiver, inbox, event processor, duplicate and out-of-order tests and demos, property-style event test.
4. Heartbeats, leases, reaper, both crash-window tests, worker-crash and crash-after-answered demos.
5. Predictive pacing, Safety Controller rules, fencing tokens, breaker, agent-drop and provider-outage demos.
6. Simulator, scenarios A to D (progressive vs predictive, Provider A vs B), charts, `SIMULATION_RESULTS.md`, answer-rate-shock demo.
7. Load test at 8, 16, 32 workers, `SCALING.md` with the measured numbers.
8. README, ARCHITECTURE, ADR, STATE_MACHINES, FINAL_ANSWER, INTERVIEW_PREP; final clean run of everything.
