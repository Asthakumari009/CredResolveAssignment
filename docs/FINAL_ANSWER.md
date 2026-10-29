# Final answer

**How would you build a SmartDialer that gets as much of the utilization benefit of predictive
dialing as possible, while retaining the deterministic safety characteristics of progressive dialing?**

Split the system into a part that is allowed to guess and a part that is not.

**Pacing guesses.** It estimates the answer rate with a Wilson upper bound, so it assumes
more people will pick up than it has seen. It then picks the largest number of extra calls for which the chance of more
answers than free agents is at most 1%. It only proposes a number with a written explanation. It
cannot dial.

**A Safety Controller decides, deterministically.** It takes its own fresh snapshot and applies
fixed rules: abandon budget with hysteresis, hard ratio cap, agent-drop and answer-rate-drift
detectors, provider health, calling window, cold start. It writes the decision and its reason
codes to a table. The only way to place a call is to spend one unit of the short-lived permit
it issues, in the same database transaction that leases the borrower and creates the call. When
anything is uncertain (too few samples, drift, a sick provider, a deposed leader), the rules fall back to
progressive, which cannot abandon. So the worst case is progressive's worst case.

**Correctness lives in the database, not in workers.** Agents, borrowers and permits are claimed with
atomic conditional updates. A fencing token on the pacing lease and a call id on every agent
reservation stop stale workers. Webhooks land in an inbox and are applied idempotently, in order, by
any worker. After a crash, nothing is guessed: the reaper asks the provider by idempotency key.

Measured in the simulator, this gains +38% utilization at a 20% answer rate with zero
abandons. Predictive helps less as answer rates rise, and when the provider loses events the abandon budget
forces progressive. That is the intended trade: safety first, then as much utilization as it allows.
