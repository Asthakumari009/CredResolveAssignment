"""Real concurrency on real Postgres: 50 OS processes race on the same row, many rounds.

Each round: the parent resets the contested row, all 50 children pass a barrier together and
fire the same statement, then the parent checks the winners. RACE_ROUNDS (default 500) sets
the number of rounds.
"""
import multiprocessing as mp
import os
import uuid

import pytest

from smartdialer.allocator.allocator import lease_borrower
from smartdialer.db import connect
from smartdialer.safety.controller import SafetyController
from smartdialer.transitions import reserve_agent
from tests.helpers import issue_permit, make_campaign

RACERS = 50
ROUNDS = int(os.environ.get("RACE_ROUNDS", "500"))
CTX = mp.get_context("fork")


def _racer(op, dsn, campaign_id, rounds, start, done, results):
    try:
        _race_loop(op, dsn, campaign_id, rounds, start, done, results)
    finally:
        results.close()
        results.join_thread()
        # Skip interpreter shutdown: it would finalize the parent's forked connection object
        # and could close the parent's socket.
        os._exit(0)


def _race_loop(op, dsn, campaign_id, rounds, start, done, results):
    with connect(dsn) as conn:
        for r in range(rounds):
            start.wait()
            try:
                with conn.transaction():
                    if op == "agent":
                        won = reserve_agent(conn, campaign_id, uuid.uuid4(), f"w{os.getpid()}") is not None
                    elif op == "borrower":
                        won = lease_borrower(conn, campaign_id, f"w{os.getpid()}", 99) is not None
                    else:
                        won = SafetyController.claim_units(conn, campaign_id, 1) is not None
                results.put((r, won, None))
            except Exception as e:   # any DB error is a test failure, reported to the parent
                results.put((r, False, repr(e)))
            done.wait()


def _race(dsn, conn, campaign_id, op, reset, winners_per_round):
    start, done = CTX.Barrier(RACERS + 1), CTX.Barrier(RACERS + 1)
    results = CTX.Queue()
    procs = [CTX.Process(target=_racer, args=(op, dsn, campaign_id, ROUNDS, start, done, results)) for _ in range(RACERS)]
    for p in procs:
        p.start()
    try:
        for r in range(ROUNDS):
            reset()
            start.wait(timeout=30)
            done.wait(timeout=30)
            outcomes = [results.get(timeout=30) for _ in range(RACERS)]
            errors = [e for _, _, e in outcomes if e]
            assert not errors, errors[:3]
            assert sum(w for _, w, _ in outcomes) == winners_per_round, f"round {r}"
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.kill()


def test_50_workers_race_for_one_agent(dsn, conn):
    cid = make_campaign(conn, agents=1)
    reset = lambda: conn.execute("UPDATE agents SET state = 'AVAILABLE', reserved_call_id = NULL WHERE campaign_id = %s", (cid,))
    _race(dsn, conn, cid, "agent", reset, winners_per_round=1)


def test_50_workers_race_for_one_borrower(dsn, conn):
    cid = make_campaign(conn, borrowers=1)
    reset = lambda: conn.execute("UPDATE borrowers SET state = 'PENDING', attempts = 0 WHERE campaign_id = %s", (cid,))
    _race(dsn, conn, cid, "borrower", reset, winners_per_round=1)


def test_50_workers_race_for_permit_units(dsn, conn):
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=10)
    reset = lambda: conn.execute("UPDATE dial_permits SET consumed_count = 0 WHERE id = %s", (pid,))
    _race(dsn, conn, cid, "permit", reset, winners_per_round=10)
    assert conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"] == 10


BATCH_ROUNDS = int(os.environ.get("BATCH_ROUNDS", "20"))


def _claimer(dsn, campaign_id, seed, start, results):
    import random
    rng = random.Random(seed)
    try:
        with connect(dsn) as conn:
            for r in range(BATCH_ROUNDS):
                start.wait()
                got, err = 0, None
                try:
                    # Keep claiming random batch sizes until the permit is used up.
                    while (c := SafetyController.claim_units(conn, campaign_id, rng.randint(1, 25))) is not None:
                        got += c["units"]
                except Exception as e:   # noqa: BLE001
                    err = repr(e)
                results.put((r, got, err))
                start.wait()   # wait for the parent to check and reset
    finally:
        results.close()
        results.join_thread()
        os._exit(0)


def test_50_workers_batch_claims_never_exceed_approved(dsn, conn):
    """50 processes claim random batch sizes (1 to 25) from one permit until it is used up.
    Total units handed out must equal approved_count exactly: never more (the safety property),
    and not fewer either (the fallback to smaller batches drains the remainder)."""
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=997)   # not a multiple of anything convenient
    start, results = CTX.Barrier(RACERS + 1), CTX.Queue()
    procs = [CTX.Process(target=_claimer, args=(dsn, cid, i, start, results)) for i in range(RACERS)]
    for p in procs:
        p.start()
    try:
        for r in range(BATCH_ROUNDS):
            start.wait(timeout=30)
            outcomes = [results.get(timeout=60) for _ in range(RACERS)]
            assert not [e for _, _, e in outcomes if e]
            total = sum(g for _, g, _ in outcomes)
            row = conn.execute("SELECT consumed_count, approved_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()
            assert total == row["consumed_count"] == row["approved_count"] == 997, f"round {r}: {total}, {row}"
            conn.execute("UPDATE dial_permits SET consumed_count = 0 WHERE id = %s", (pid,))
            start.wait(timeout=30)
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.kill()
