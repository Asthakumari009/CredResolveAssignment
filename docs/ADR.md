# Architecture decision records

Format for each: **Chosen**, **Why**, **Problem it solves**, **What it makes harder**.

## ADR-1: PostgreSQL is the only source of truth. No Redis, Kafka or cache

- **Chosen:** All coordination state lives in Postgres: agent reservations, borrower leases,
  permits, the event inbox, leader leases, breaker state. Workers keep nothing important in memory.
- **Why:** Every race in a dialer is about one specific row: one agent, one borrower, one permit.
  Postgres settles races on a row with one atomic statement: a conditional UPDATE, a row lock,
  SKIP LOCKED, or a unique index. A second store would add a second source of truth and the
  question "which one is right after a crash?".
- **Problem it solves:** Any worker can crash at any instruction, and the state on disk is still
  consistent and complete. The invariants are checkable from the history tables alone.
- **What it makes harder:** Throughput is bounded by hot rows. The permit row is updated by every
  batch claim in a campaign, and the `provider_health` row by every evaluation. `SCALING.md` measures
  this and describes the fixes. Every read is a query, so the snapshot query has to stay cheap.

## ADR-2: Webhooks go into an inbox table, and workers apply them

- **Chosen:** The webhook receiver only does `INSERT ... ON CONFLICT (provider, event_id) DO NOTHING`
  and returns 200. Any worker claims inbox rows and applies them through the call state machine,
  in per-call order (sequence, id), with a 1500 ms hold for sequence gaps.
- **Why:** An event is never "for" a particular process. If the worker that placed the call dies,
  another one processes its events. Receiving and applying are decoupled, so a slow apply never
  makes the provider retry.
- **Problem it solves:** At-least-once delivery from providers (dedupe on the unique key), out-of-order
  delivery (ranks plus FIFO), worker crashes between receiving and applying (the row stays
  unprocessed), and an audit trail of every event and what it did (`processing_result`).
- **What it makes harder:** One extra hop of latency (measured p50 in `SCALING.md`). The inbox
  grows and needs partitioning or pruning at scale. Per-call ordering needs the resequencing hold,
  which delays out-of-order events by up to 1.5 s.

## ADR-3: Leases with fencing tokens, not locks held across work

- **Chosen:** Pacing leadership is a lease on the campaign row with a `fencing_token` that is
  bumped on every takeover. Permits carry the token, and consuming a permit checks it against the
  campaign's current token. Agents are fenced by `reserved_call_id`: every worker-driven agent
  transition after reservation includes `WHERE reserved_call_id = <call>`.
- **Why:** A paused process (GC, network partition, a slow provider call) can wake up believing it
  still leads. A lease alone cannot stop it from acting. A fencing token checked at the point of
  use does. Call ids are UUIDs and never reused, so agent fencing has no ABA problem and needs no
  version column.
- **Problem it solves:** Two leaders issuing permits at once, and a stale worker moving an agent
  that has since been given to another call (`tests/test_agent_fencing.py` tries each transition).
- **What it makes harder:** Leadership changes take up to one lease period. At takeover there is a
  small window where the old leader's last permit was consumed and the new leader's snapshot was
  taken just before. That can overcount by at most one tick's worth of calls. The hard ratio cap bounds it.

## ADR-4: Dial permits: pacing proposes, the Safety Controller authorizes

- **Chosen:** Pacing returns a number and an explanation and nothing else. The Safety Controller
  revokes the previous permit, takes its own snapshot, applies the rules, logs the decision to
  `safety_decisions`, and inserts one `dial_permits` row (approved count, 2 s expiry, token).
  Workers **claim** units in batches of up to 10 with one short statement
  (`consumed_count = consumed_count + k ... AND consumed_count + k <= approved_count`, falling back to
  smaller k), outside any other transaction. Each call transaction **pins** the permit
  (`FOR KEY SHARE`, valid only if unrevoked, unexpired and current-token), then leases the borrower,
  reserves the agent and inserts the call. A call needs both a claimed unit and a live pin.
- **Why:** The safety boundary is structural, not a convention. There is no code path from pacing to a
  dial. `CallAllocator` refuses to be constructed without a `SafetyController`, and
  `tests/test_architecture.py` checks the imports.
- **Problem it solves:** A pacing bug (or a stale snapshot) cannot exceed what the rules allow.
  "Why did we dial 17 and not 10?" is answered by one row: proposal, snapshot, reason codes, outcome.
- **Why unused claimed units are dropped, not returned:** A worker may claim 10 units and only use 3
  (no free agent, no due borrower, or the permit was revoked first). Those 7 are never given back:
  they just die with the permit. This is safe because each unit can turn into at most one call, and
  only through a transaction that pinned a live permit. So for every permit,
  `calls <= claimed <= approved`. The second inequality is enforced by the claim's WHERE clause and a CHECK
  constraint. The first holds because a unit is spent at most once by its own worker: it is deducted before the
  call transaction and given back only on an explicit rollback, so even a COMMIT whose acknowledgement
  is lost cannot let one unit become two calls. A dropped unit
  can only make the number of calls *smaller* than approved, never larger. The cost is
  underdialing: measured at 0.2% to 4% of claimed units (`SCALING.md`). Returning units would need another
  write to the hot row and a way to return units from a crashed worker. Neither buys any safety.
- **Why the revoke is two steps:** Batching means call transactions no longer hold the permit row
  lock, so revoking no longer waits for them by itself. `decide` therefore (1) retires the live permit
  in its own short transaction, after which no new pin can succeed, then (2) takes `FOR UPDATE` on
  the retired row, which waits for exactly the pins taken before step 1. Only then does it read its
  snapshot. So every call the old permit will ever produce is counted. Pins taken after step 1 fail,
  so the waiter set is finite and the decider cannot be starved
  (`tests/test_permits.py::test_decider_waits_for_pinned_calls_and_counts_them`).
- **What it makes harder:** One more round trip per tick, and one permit row per campaign that every
  claim updates (short, once per batch: about 2 waiters at 32 workers, `SCALING.md`). The invariant is
  `calls <= claimed` rather than `calls == consumed`, so "how many calls did this permit make" is a count
  of calls, not a column. The 2 s expiry means a slow leader starves dispatch rather than overdials.
  That is the right failure direction.

## ADR-5: Python 3.12

- **Chosen:** Python, psycopg 3 with plain SQL, FastAPI only at the HTTP edges, multiprocessing
  for workers.
- **Why:** Fastest to write and to read in an interview. All the correctness lives in SQL
  statements and pure functions, which do not depend on the language. Workers are separate processes,
  so the GIL does not limit the coordination being tested.
- **Problem it solves:** A small, readable prototype where every decision is one function.
- **What it makes harder:** Per-process throughput is low (the load test shows a worker loop doing ~15
  dispatches/s), and the mock provider needed a thread pool to deliver webhooks fast enough. In
  production the worker loop would be async or in a compiled language, and the SQL would stay the same.

## ADR-6: The simulator shares pure functions, not a repository layer (approval condition C1)

- **Chosen:** Every decision (call and agent state machines, progressive and predictive pacing,
  safety rules, breaker, backoff) is a pure function. The live worker and the discrete-event
  simulator both call the same functions. The simulator keeps its own in-memory world and a
  virtual clock. There is no `Repository` interface with Postgres and in-memory implementations.
- **Why:** The thing worth sharing is the decisions, and they are shared exactly. A second
  repository plus a contract suite would roughly double the persistence code.
- **Problem it solves:** One hour of dialing simulates in about 2 s, deterministically (seed 7),
  and measures the real pacing and safety code.
- **What it makes harder (the tradeoff C1 asks to state):** The **orchestration loop exists twice**:
  `worker/main.py` (dispatch, events, answer hold, reaper, against Postgres) and `sim/simulator.py`
  (the same sequence against memory). They can drift. A change to the order of operations in one
  is not automatically reflected in the other, and the simulator never exercises SQL, locks or
  crash windows. Mitigations: both loops are kept as thin as possible (they only sequence calls into
  shared functions; the worker docstring says so), and the SQL path is covered separately by the
  concurrency tests, the demos and the load test. If the loops grow, the fix is the repository
  abstraction this ADR declined.

## ADR-7: Age-based reconciliation, no per-row reaper leases

- **Chosen:** The reaper selects calls that have been in a state too long (pre-dial > 5 s,
  INITIATED or RINGING with no news > 60 s) and asks the provider `get_call_status(idempotency key)`.
  It turns the answer into a synthetic inbox event (`reconcile:<call>:<status>`) that goes through
  the normal event path. NOT_FOUND becomes CANCELLED(NOT_DIALED). A provider unreachable for
  `reconcile_give_up_s` becomes FAILED(TIMEOUT). Polls run in parallel and are skipped while the breaker
  is OPEN.
- **Why:** The provider's answer is idempotent, and the inbox dedupes on the synthetic event id, so two
  reapers polling the same call cannot double-apply. Per-row leases would add a lease column,
  expiry logic and a second crash window for no extra safety.
- **Problem it solves:** Both crash windows (C3): a crash after commit before dial (provider says NOT_FOUND)
  and a crash after the provider accepted, before INITIATED was recorded (provider says RINGING
  or later). Neither is guessed. Both are asked.
- **What it makes harder:** Duplicate polling under load (harmless, but it is provider traffic). Two
  known residual risks. First, a paused (not dead) worker can dial after reconciliation already
  got NOT_FOUND. It then sees the call CANCELLED and hangs up at once, so the borrower may hear one
  ring that is cut off. Second, the TIMEOUT give-up may fail a call the provider did place. Its events still
  arrive and are STALE_IGNORED.

## ADR-8: No real provider (Plivo skipped)

- **Chosen:** Two mock providers (A: fast, 1% errors. B: slow, timeouts, duplicates, reordering,
  drops, outages) behind the same `Provider` protocol a real client would implement.
- **Why:** The point of the prototype is the control plane: races, recovery, pacing safety.
  A real provider adds credentials, cost and real phone calls without testing anything new, and its
  failure modes cannot be triggered on demand. The mock's can.
- **Problem it solves:** Every failure scenario is reproducible by a flag (`/control`), so each demo is deterministic.
- **What it makes harder:** Real providers have quirks the mock does not (event schemas, signature
  verification on webhooks, rate limits). Plugging one in means one `Provider` implementation plus
  webhook signature checks in `webhook_api.py`.

## ADR-9: All Python runs inside Docker (approval condition C8)

- **Chosen:** `docker compose` runs Postgres 16 and an `app` image. `make` targets and
  `tasks.py` are thin wrappers over `docker compose run --rm app ...`.
- **Why:** The development machine is Windows, where `make`, signals and multiprocessing behave
  differently. One Linux image gives everyone the same Python, the same `SIGKILL`, and the same `fork`.
- **Problem it solves:** "Works on my machine". Crash demos use real in-container SIGKILL.
- **What it makes harder:** A container start per command (a few seconds), and debugging with a
  local IDE needs a remote interpreter or `make shell`.

## ADR-10: READ COMMITTED with explicit row locks, not SERIALIZABLE

- **Chosen:** Default isolation. Each race is closed by a conditional UPDATE (Postgres re-checks
  the WHERE clause on the latest row version after waiting for the lock) or by SKIP LOCKED, and
  every multi-row transaction follows one lock order.
- **Why:** The races are on known rows, and each has a specific guard with a `# CONCURRENCY:` comment.
  SERIALIZABLE would add retry loops everywhere and hide which statement does the protecting.
- **Problem it solves:** Correctness that can be explained statement by statement, and no
  serialization-failure retries under load.
- **What it makes harder:** The lock order must be kept by hand. Foreign key checks take implicit
  locks too, and the load test found a deadlock there (PLAN D10, now covered by a regression test).
