"""Builds a campaign and runs one real pacing tick, so demo calls go through the same
pacing -> Safety Controller -> permit -> allocator path as production."""
import json

from smartdialer.agent_actions import login_all
from smartdialer.allocator.allocator import CallAllocator
from smartdialer.config import load_settings
from smartdialer.pacing import progressive
from smartdialer.providers.client import HttpProvider
from smartdialer.safety.controller import SafetyController
from smartdialer.safety.leader import try_lead
from smartdialer.safety.snapshot import load_snapshot
from demos.common import MOCK_URL, say

ALWAYS_OPEN = {"window_start": "00:00", "window_end": "00:00"}


def campaign(conn, *, mode="PROGRESSIVE", provider="A", agents=1, borrowers=1, settings=None) -> int:
    cid = conn.execute("INSERT INTO campaigns (name, mode, provider_name, settings) VALUES ('demo', %s, %s, %s) RETURNING id",
                       (mode, provider, json.dumps({**ALWAYS_OPEN, **(settings or {})}))).fetchone()["id"]
    conn.execute("INSERT INTO agents (campaign_id, name) SELECT %s, 'agent-' || g FROM generate_series(1, %s) g", (cid, agents))
    conn.execute("INSERT INTO borrowers (campaign_id, phone) SELECT %s, '+91 98' || lpad(g::text, 8, '0') FROM generate_series(1, %s) g",
                 (cid, borrowers))
    login_all(conn, cid)
    return cid


def progressive_tick(conn, cid, worker="demo-worker", provider="A", timeout_s=3.0) -> list[dict]:
    """One pacing tick + dispatch, in-process."""
    cfg = load_settings(conn, cid)
    token = try_lead(conn, cid, worker, cfg.leader_lease_ms)
    proposal = progressive.propose(load_snapshot(conn, cid, cfg, worker, token))
    safety = SafetyController(worker)
    decision = safety.decide(conn, cid, token, proposal)
    say(f"pacing proposed {proposal.requested_calls}; Safety Controller: {decision.outcome} {decision.approved_calls}"
        f" (permit #{decision.permit_id})")
    alloc = CallAllocator(safety, {provider: HttpProvider(provider, MOCK_URL, timeout_s)}, worker)
    return alloc.dispatch(conn, cid, max_calls=decision.approved_calls, max_attempts=cfg.max_attempts)
