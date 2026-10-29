import uuid

from smartdialer.domain.breaker import BreakerConfig, next_state
from smartdialer.domain.types import PacingProposal
from smartdialer.safety import breaker_store
from smartdialer.safety.controller import SafetyController
from smartdialer.safety.leader import try_lead
from smartdialer.safety.snapshot import load_snapshot
from smartdialer.config import load_settings
from tests.helpers import make_campaign


def test_breaker_transitions():
    c = BreakerConfig()
    assert next_state("CLOSED", 0, 0, 5, 5, 0, 0, c) == "OPEN"
    assert next_state("CLOSED", 0, 0, 7, 3, 0, 0, c) == "DEGRADED"
    assert next_state("CLOSED", 0, 0, 1, 4, 0, 0, c) == "CLOSED", "too few calls to judge"
    assert next_state("DEGRADED", 0, 0, 10, 0, 0, 0, c) == "CLOSED"
    assert next_state("OPEN", 0, 5, 0, 0, 0, 0, c) == "OPEN"
    assert next_state("OPEN", 0, 10, 0, 0, 0, 0, c) == "HALF_OPEN"
    assert next_state("HALF_OPEN", 0, 0, 0, 0, 2, 0, c) == "HALF_OPEN"
    assert next_state("HALF_OPEN", 0, 0, 0, 0, 3, 0, c) == "CLOSED"
    assert next_state("HALF_OPEN", 0, 0, 0, 0, 2, 1, c) == "OPEN"


def test_breaker_store_opens_on_failures(conn):
    cid = make_campaign(conn, borrowers=20)
    bids = [r["id"] for r in conn.execute("SELECT id FROM borrowers")]
    for i, b in enumerate(bids):
        failed = i % 4 != 0   # 75% timeouts
        conn.execute(
            "INSERT INTO calls (id, campaign_id, borrower_id, mode, state, state_rank, provider_name, failure_reason, provider_call_id)"
            " VALUES (%s, %s, %s, 'PREDICTIVE', %s, 9, 'A', %s, %s)",
            (uuid.uuid4(), cid, b, "FAILED" if failed else "COMPLETED", "TIMEOUT" if failed else None,
             None if failed else f"p{i}"))
    assert breaker_store.evaluate(conn, cid) == "OPEN"
    assert conn.execute("SELECT breaker_state FROM provider_health WHERE provider_name = 'A'").fetchone()["breaker_state"] == "OPEN"


def _decide(conn, cid, worker, token, n=5):
    cfg = load_settings(conn, cid)
    s = load_snapshot(conn, cid, cfg, worker, token)
    return SafetyController(worker).decide(conn, cid, token, PacingProposal(n, "PROGRESSIVE", "p", s.taken_at))


def test_decision_is_logged_with_inputs_and_permit_issued(conn):
    cid = make_campaign(conn, agents=3)
    token = try_lead(conn, cid, "leader", 3000)
    d = _decide(conn, cid, "leader", token)
    assert (d.outcome, d.approved_calls) == ("REDUCE", 3)
    row = conn.execute("SELECT * FROM safety_decisions WHERE id = %s", (d.decision_id,)).fetchone()
    assert row["requested"] == 5 and row["approved_count"] == 3 and row["reason_codes"] == ["PROGRESSIVE_BUDGET"]
    assert row["snapshot"]["available"] == 3 and "available=3" in row["explanation"]
    p = conn.execute("SELECT * FROM dial_permits WHERE id = %s", (d.permit_id,)).fetchone()
    assert p["approved_count"] == 3 and p["fencing_token"] == token and not p["revoked"]


def test_non_leader_is_rejected_and_cannot_revoke_the_leaders_permit(conn):
    cid = make_campaign(conn, agents=3)
    token = try_lead(conn, cid, "leader", 3000)
    good = _decide(conn, cid, "leader", token)
    bad = _decide(conn, cid, "impostor", token)
    assert bad.outcome == "REJECT" and bad.reason_codes == ("NOT_PACING_LEADER",) and bad.permit_id is None
    assert not conn.execute("SELECT revoked FROM dial_permits WHERE id = %s", (good.permit_id,)).fetchone()["revoked"]


def test_new_decision_revokes_previous_permit(conn):
    cid = make_campaign(conn, agents=3)
    token = try_lead(conn, cid, "leader", 3000)
    first = _decide(conn, cid, "leader", token)
    second = _decide(conn, cid, "leader", token)
    assert conn.execute("SELECT revoked FROM dial_permits WHERE id = %s", (first.permit_id,)).fetchone()["revoked"]
    assert conn.execute("SELECT count(*) AS n FROM dial_permits WHERE NOT revoked").fetchone()["n"] == 1
    assert second.permit_id != first.permit_id
