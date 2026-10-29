# SmartDialer

A prototype outbound dialer control plane for collections: progressive and predictive dialing
across many worker processes, coordinated only through PostgreSQL, with a Safety Controller that
must approve every dial. Built for the CredResolve take-home (`ASSIGNMENT.md`).

Start with [docs/FINAL_ANSWER.md](docs/FINAL_ANSWER.md) (under 300 words), then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- Docker Desktop, running (`docker info` must succeed, not just `docker --version`).
- `make` (optional). Every target also works as `python tasks.py <target>`, which only shells out
  to `docker compose`. No project Python runs on the host.

## Setup

```bash
make up
```

```bash
make migrate
```

`make up` starts Postgres 16 (host port 55432) and builds the `app` image. All Python (tests,
workers, mock provider, webhook receiver, simulator, load test) runs inside `app`.

## Commands

| Command | What it does |
|---|---|
| `make test` | Full test suite: state machines, 50-process race tests, fencing, permits, crash windows, event handling, pacing, safety rules, simulator |
| `make sim` | Simulator, scenarios A to F, progressive vs predictive, Provider A vs B. Writes `docs/SIMULATION_RESULTS.md` and `docs/img/` |
| `make loadtest` | 1,000 agents, 50,000 borrowers, 8 workers, 60 s. `make loadtest ARGS="--workers 8 16 32"` for the sweep in `docs/SCALING.md` |
| `make demo-duplicate-events` | Same webhook delivered many times: applied once |
| `make demo-out-of-order` | Events arrive COMPLETED, ANSWERED, RINGING: the call ends COMPLETED, late events are ignored |
| `make demo-worker-crash` | Worker SIGKILLs itself after commit before dial, and after dial before INITIATED. Another worker reconciles both by idempotency key |
| `make demo-crash-after-answered` | Worker SIGKILLs itself while applying ANSWERED (before and after COMMIT); another worker carries on, same final state |
| `make demo-agent-drop` | 40 of 100 agents go offline within 3 s during predictive dialing: ratio cap on the next tick, then forced progressive |
| `make demo-provider-outage` | Provider A goes down: breaker opens, dialing stops, HALF_OPEN probes, recovery |
| `make demo-answer-rate-shock` | Answer rate jumps 20% to 70% (and drops 70% to 10%), in the simulator: decision-by-decision table |
| `make psql` | psql into the dialer database | |
| `make shell` | bash in the app container | |
| `make down` | Stop and delete the database volume | |

Every demo ends with the invariant checks over the history tables and prints `RESULT: PASS` or `FAIL`.

Windows without make: `python tasks.py test`, `python tasks.py demo-worker-crash`,
`python tasks.py loadtest --workers 8 16 32`.

## Repository map

```
smartdialer/
  domain/        pure: state machines, safety rules, stats, breaker, backoff, calling window
  pacing/        pure: progressive and predictive proposals
  safety/        Safety Controller, snapshot, pacing leader lease, breaker store
  allocator/     permit -> borrower -> agent -> call in one transaction, dial after commit
  events/        webhook receiver (inbox), event processor, answer hold
  recovery/      reaper (reconciliation), heartbeat
  worker/        the worker loop: sequences the above, no logic of its own
  transitions.py the only path that changes call or agent state
  invariants.py  checks run after every demo, test and load run
mock_provider/   Providers A and B with fault injection (/control)
sim/             discrete-event simulator reusing the pure modules
loadtest/        multi-process load test
demos/           one script per failure scenario
migrations/      plain SQL
tests/
docs/            ARCHITECTURE, STATE_MACHINES, ADR, SCALING, SIMULATION_RESULTS, FINAL_ANSWER, INTERVIEW_PREP
```

## Ground rules this code keeps

- PostgreSQL is the only source of truth. No cache, no queue.
- Every race is closed by the database, and each such place has a `# CONCURRENCY:` comment.
- Decision logic is pure functions, shared by the workers and the simulator.
- Pacing cannot dial. Only a permit from the Safety Controller can, and every decision is logged
  to `safety_decisions` with its reason codes.
