"""Permits (C4): claim only from a live, unrevoked, unexpired, current-token permit, and spend a
claimed unit only while the permit is still live (pin)."""
import threading
import time
from datetime import datetime, timezone

from smartdialer.db import connect
from smartdialer.domain.types import PacingProposal
from smartdialer.safety.controller import SafetyController
from smartdialer.safety.leader import try_lead
from tests.helpers import issue_permit, make_campaign

claim, pin = SafetyController.claim_units, SafetyController.pin


def consumed(conn, pid):
    return conn.execute("SELECT consumed_count FROM dial_permits WHERE id = %s", (pid,)).fetchone()["consumed_count"]


def test_claim_until_exhausted(conn):
    cid = make_campaign(conn)
    issue_permit(conn, cid, approved=2)
    assert claim(conn, cid, 1) and claim(conn, cid, 1)
    assert claim(conn, cid, 1) is None


def test_claim_falls_back_to_smaller_batch(conn):
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=7)
    assert claim(conn, cid, 10)["units"] == 5, "10 does not fit, 5 does"
    assert claim(conn, cid, 10)["units"] == 2
    assert claim(conn, cid, 10) is None
    assert consumed(conn, pid) == 7


def test_revoked_permit_cannot_be_claimed(conn):
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=5)
    conn.execute("UPDATE dial_permits SET revoked = true WHERE id = %s", (pid,))
    assert claim(conn, cid, 1) is None
    assert consumed(conn, pid) == 0


def test_expired_permit_cannot_be_claimed(conn):
    cid = make_campaign(conn)
    issue_permit(conn, cid, approved=5, ttl_ms=-1)
    assert claim(conn, cid, 1) is None


def test_deposed_leaders_permit_dies_on_takeover(conn):
    cid = make_campaign(conn)
    token = try_lead(conn, cid, "w1", lease_ms=1)
    pid = issue_permit(conn, cid, approved=5, token=token)
    assert claim(conn, cid, 1)
    conn.execute("SELECT pg_sleep(0.01)")
    new_token = try_lead(conn, cid, "w2", lease_ms=3000)    # w1's lease expired: takeover
    assert new_token == token + 1
    assert claim(conn, cid, 1) is None, "w1's permit carries the old token"
    assert pin(conn, pid) is None, "units w1 already claimed cannot be spent either"


def test_claimed_units_die_with_their_permit(conn):
    """Units claimed before a revoke or expiry cannot be spent after it."""
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=5)
    assert claim(conn, cid, 5)["units"] == 5
    assert pin(conn, pid)
    issue_permit(conn, cid, approved=5)          # next decision revokes pid
    assert pin(conn, pid) is None
    expiring = issue_permit(conn, cid, approved=5, ttl_ms=-1)
    assert pin(conn, expiring) is None


def test_lease_is_exclusive_and_renewable(conn):
    cid = make_campaign(conn)
    t1 = try_lead(conn, cid, "w1", lease_ms=3000)
    assert try_lead(conn, cid, "w2", lease_ms=3000) is None
    assert try_lead(conn, cid, "w1", lease_ms=3000) == t1, "renewal keeps the token"


def test_claim_does_not_wait_for_pinned_calls(conn, dsn):
    """The point of the batch design: a call transaction holding its pin must not block other
    workers from claiming units."""
    cid = make_campaign(conn)
    pid = issue_permit(conn, cid, approved=10)
    with conn.transaction():
        assert pin(conn, pid)
        with connect(dsn) as c2:
            c2.execute("SET lock_timeout = '1s'")
            assert claim(c2, cid, 3)["units"] == 3


def _decide_in_thread(dsn, cid, token, errors):
    proposal = PacingProposal(1, "PROGRESSIVE", "test", datetime.now(timezone.utc))

    def run():
        try:
            with connect(dsn) as c2:
                SafetyController("leader").decide(c2, cid, token, proposal)
        except Exception as e:   # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_decider_waits_for_pinned_calls_and_counts_them(conn, dsn):
    """A call transaction pinned the permit before the decider retired it. The decider must wait
    for it (so its snapshot counts the call) and must not deadlock with the call's foreign key
    checks on the campaign row (PLAN D10)."""
    cid = make_campaign(conn, agents=1, borrowers=1)
    token = try_lead(conn, cid, "leader", lease_ms=60_000)
    pid = issue_permit(conn, cid, approved=5, token=token)
    errors = []
    with conn.transaction():
        assert pin(conn, pid)
        t = _decide_in_thread(dsn, cid, token, errors)
        time.sleep(0.5)
        assert t.is_alive(), "decider is waiting for this pinned call"
        conn.execute("SET LOCAL lock_timeout = '3s'")
        b = conn.execute("SELECT id FROM borrowers").fetchone()["id"]
        conn.execute("""INSERT INTO calls (id, campaign_id, borrower_id, mode, state, state_rank, provider_name, permit_id)
                        VALUES (gen_random_uuid(), %s, %s, 'PREDICTIVE', 'QUEUED', 0, 'A', %s)""", (cid, b, pid))
    t.join(timeout=10)
    assert not errors, errors
    snap = conn.execute("SELECT snapshot FROM safety_decisions ORDER BY id DESC LIMIT 1").fetchone()["snapshot"]
    assert snap["pre_dial"] == 1, "the call committed while the decider waited is in its snapshot"
    assert pin(conn, pid) is None, "retired permit can no longer be spent"
