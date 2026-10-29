"""Call Allocator: turns permit units into calls. It cannot dial without a permit.

Permit units are claimed in small batches (SafetyController.claim_units), one short statement
that holds the permit row lock only for itself. The worker keeps the claimed units and spends
them one call at a time. Per call, one transaction (LOCK ORDER: permit -> borrower -> agent;
the call row is new):

    1. pin the permit: still live?        (SafetyController.pin, FOR KEY SHARE, no contention)
    2. lease the next due borrower        (FOR UPDATE SKIP LOCKED)
    3. progressive only: reserve an agent (atomic AVAILABLE -> RESERVED)
    4. insert the call row with a fresh UUID (the provider idempotency key)

All or nothing: if any step finds nothing, the transaction rolls back. The claimed unit is not
returned to the permit; it stays with this worker for its next call, and is dropped when the
permit is revoked or expires. Dropped units mean fewer calls than approved, never more.

The agent becomes RESERVED at the same instant a call row exists for it, and both are durable
BEFORE the provider is contacted, so a crash at any later point leaves a row that
reconciliation can resolve by asking the provider about that idempotency key.

The provider call happens after COMMIT, never inside a transaction: holding row locks across a
2 s Provider B round trip would serialize the whole campaign.
"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

from smartdialer.domain.call_sm import CallState as S
from smartdialer.faults import crash_point
from smartdialer.providers.interface import DialAccepted, DialRejected, Provider
from smartdialer.safety.controller import SafetyController
from smartdialer.transitions import advance_call, record_creation, reserve_agent

CLAIM_BATCH = 10   # units claimed per permit-row statement; leftovers are dropped on revoke


def lease_borrower(conn, campaign_id: int, worker_id: str, max_attempts: int):
    """# CONCURRENCY: SKIP LOCKED hands each due borrower to exactly one transaction; others skip
    it and take the next. The state = 'PENDING' re-check in the UPDATE makes the claim atomic
    even without SKIP LOCKED. Which borrower: the one that has been due the longest."""
    return conn.execute(
        """UPDATE borrowers SET state = 'IN_PROGRESS', attempts = attempts + 1,
               locked_by_worker = %(w)s, updated_at = clock_timestamp()
           WHERE id = (SELECT id FROM borrowers
                       WHERE campaign_id = %(c)s AND state = 'PENDING' AND next_attempt_at <= now()
                         AND attempts < %(max)s
                       ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
             AND state = 'PENDING'
           RETURNING id, phone""", {"w": worker_id, "c": campaign_id, "max": max_attempts}).fetchone()


class CallAllocator:
    def __init__(self, safety: SafetyController, providers: dict[str, Provider], worker_id: str, dial_threads: int = 16):
        if not isinstance(safety, SafetyController):
            raise TypeError("CallAllocator requires a SafetyController: there is no way to dial without permits")
        self.safety = safety
        self.providers = providers
        self.worker_id = worker_id
        self.pool = ThreadPoolExecutor(max_workers=dial_threads, thread_name_prefix="dial")
        self.held: dict[int, dict] = {}   # campaign -> claimed units not yet spent {"id", "mode", "units"}
        self.reserve_latencies_ms: list[float] = []   # dispatch transaction time per created call (load test)

    def dispatch(self, conn, campaign_id: int, max_calls: int, max_attempts: int) -> list[dict]:
        camp = conn.execute("SELECT provider_name FROM campaigns WHERE id = %s", (campaign_id,)).fetchone()
        created = []
        retries = 0
        while len(created) < max_calls:
            t0 = time.perf_counter()
            held = self.held.get(campaign_id)
            if not held or held["units"] == 0:
                held = self.safety.claim_units(conn, campaign_id, min(CLAIM_BATCH, max_calls - len(created)))
                if held is None:
                    break   # no live permit, or it is used up
                self.held[campaign_id] = held
            call = self._create_call(conn, campaign_id, camp["provider_name"], max_attempts, held)
            if call == "STALE":
                self.held.pop(campaign_id, None)   # permit revoked or expired: drop the units
                retries += 1
                if retries > 1:
                    break
                continue
            if call is None:
                break   # no borrower or no agent right now; the unit was given back
            self.reserve_latencies_ms.append((time.perf_counter() - t0) * 1000)
            created.append(call)
        if created:
            crash_point("after_commit_before_dial")
            provider = self.providers[camp["provider_name"]]
            # Dials run in parallel threads; threads only do HTTP. All DB writes stay on this thread.
            results = list(self.pool.map(lambda c: provider.dial(str(c["id"]), c["phone"]), created))
            crash_point("after_dial_before_initiated")
            for call, result in zip(created, results):
                self.record_dial_result(conn, call["id"], result, provider)
        return created

    def _create_call(self, conn, campaign_id, provider_name, max_attempts, held):
        # Spend the unit before the transaction and give it back only on our own Rollback, when
        # nothing was committed. If COMMIT itself fails (say the connection drops), the call may
        # exist: the unit stays spent, so a unit can never turn into two calls.
        held["units"] -= 1
        call = None
        with conn.transaction():
            # LOCK ORDER: permit -> borrower -> agent
            if self.safety.pin(conn, held["id"]) is None:
                call = "STALE"
                raise psycopg.Rollback()
            borrower = lease_borrower(conn, campaign_id, self.worker_id, max_attempts)
            if borrower is None:
                held["units"] += 1
                raise psycopg.Rollback()
            call_id = uuid.uuid4()
            agent_id = None
            if held["mode"] == "PROGRESSIVE":
                agent_id = reserve_agent(conn, campaign_id, call_id, self.worker_id)
                if agent_id is None:
                    held["units"] += 1
                    raise psycopg.Rollback()   # no agent: un-lease the borrower
            state = S.RESERVED if agent_id else S.QUEUED
            conn.execute(
                """INSERT INTO calls (id, campaign_id, borrower_id, agent_id, mode, state, state_rank,
                       provider_name, owner_worker, permit_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (call_id, campaign_id, borrower["id"], agent_id, held["mode"], state.value,
                 1 if agent_id else 0, provider_name, self.worker_id, held["id"]))
            record_creation(conn, call_id, state.value, self.worker_id)
            call = {"id": call_id, "phone": borrower["phone"], "agent_id": agent_id}
        return call

    def record_dial_result(self, conn, call_id, result, provider: Provider):
        if isinstance(result, DialAccepted):
            with conn.transaction():
                verdict, row = advance_call(conn, call_id, S.INITIATED, worker_id=self.worker_id,
                                            provider_call_id=result.provider_call_id)
            if row["state"] == S.CANCELLED:
                # Reconciliation decided this call was never placed (we were too slow) and has
                # already released the agent and borrower. The provider has it after all: hang up.
                provider.hangup(result.provider_call_id, "cancelled before dial was recorded")
        elif isinstance(result, DialRejected):
            with conn.transaction():
                advance_call(conn, call_id, S.FAILED, worker_id=self.worker_id, reason="PROVIDER_ERROR")
        # DialUncertain: record nothing. The call stays QUEUED/RESERVED and the reaper reconciles
        # it via get_call_status(call_id) after reconcile_pre_dial_s. Never redial blindly.
