"""Recovery reaper: any worker, periodically.

Design choice: the reaper does not need to know whether a worker is dead. A call that has sat
in an uncertain state for too long is reconciled against the provider, which is the authority
on whether a call exists. The provider's answer is written into the inbox as an ordinary event
(event id "reconcile:<call>:<status>", so repeated polls dedupe), and the normal event processor
applies it. Recovery therefore uses exactly the same, already-tested path as live events.

    pre-dial (QUEUED/RESERVED) > reconcile_pre_dial_s     W1 crash, W2 crash, or an uncertain dial
        provider: NOT_FOUND  -> CANCELLED(NOT_DIALED): agent and borrower released, no attempt counted
        provider: has it     -> INITIATED/RINGING/...: we adopt it and carry on (no redial)
    in flight (INITIATED/RINGING) > reconcile_in_flight_s  missing events, outage
    CONNECTED > reconcile_connected_s                      missing COMPLETED
    provider unreachable for > reconcile_give_up_s (not yet answered): FAILED(TIMEOUT)

Never redial blindly: a new dial would use a new call id, and the borrower could be called twice.
"""
import json
import logging

from smartdialer.config import CampaignSettings
from smartdialer.providers.interface import ProviderUnavailable
from smartdialer.transitions import advance_call, set_agent_state

log = logging.getLogger(__name__)
D = CampaignSettings()


def reconcile_calls(conn, providers: dict, worker_id: str, limit: int = 50) -> list[tuple[str, str]]:
    # CONCURRENCY: claiming with SKIP LOCKED plus the last_reconciled_at bump means two reapers do
    # not poll the same call in the same pass. Even if they did, polling is read-only and the
    # synthetic event id is deterministic, so the inbox would drop the second copy.
    claimed = conn.execute(
        """UPDATE calls c SET last_reconciled_at = now(), reconcile_attempts = c.reconcile_attempts + 1
           FROM campaigns k
           WHERE c.id IN (
               SELECT c2.id FROM calls c2 JOIN campaigns k2 ON k2.id = c2.campaign_id
               WHERE c2.state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED','ANSWERED')
                 AND (c2.last_reconciled_at IS NULL OR c2.last_reconciled_at < now() - interval '2 seconds')
                 AND c2.state_changed_at < now() - make_interval(secs => CASE
                       WHEN c2.state IN ('QUEUED','RESERVED')
                            THEN COALESCE((k2.settings->>'reconcile_pre_dial_s')::float, %(pre)s)
                       WHEN c2.state IN ('INITIATED','RINGING')
                            THEN COALESCE((k2.settings->>'reconcile_in_flight_s')::float, %(fly)s)
                       ELSE COALESCE((k2.settings->>'reconcile_connected_s')::float, %(con)s) END)
               ORDER BY c2.state_changed_at LIMIT %(limit)s FOR UPDATE OF c2 SKIP LOCKED)
             AND k.id = c.campaign_id
           RETURNING c.id, c.provider_name, c.state, c.provider_call_id,
                     EXTRACT(EPOCH FROM now() - c.state_changed_at) AS stuck_s,
                     COALESCE((k.settings->>'reconcile_give_up_s')::float, %(give_up)s) AS give_up_s""",
        {"pre": D.reconcile_pre_dial_s, "fly": D.reconcile_in_flight_s, "con": D.reconcile_connected_s,
         "give_up": D.reconcile_give_up_s, "limit": limit}).fetchall()

    done = []
    for c in claimed:
        provider = providers.get(c["provider_name"])
        if provider is None:
            continue
        try:
            st = provider.get_call_status(str(c["id"]))
            etype = st.status
            if etype == "NOT_FOUND" and c["state"] not in ("QUEUED", "RESERVED"):
                etype = "FAILED"   # we know it was dialed, but the provider lost it
        except ProviderUnavailable as e:
            if c["stuck_s"] < c["give_up_s"] or c["state"] == "CONNECTED":
                log.info("reconcile %s: provider unavailable (%s), will retry", c["id"], e)
                continue
            st, etype = None, "TIMEOUT"   # ponytail: may orphan a call the provider did place; see INTERVIEW_PREP
        conn.execute(
            """INSERT INTO provider_events (provider_name, provider_event_id, call_id, provider_call_id, event_type, payload)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (provider_name, provider_event_id) DO NOTHING""",
            (c["provider_name"], f"reconcile:{c['id']}:{etype}", c["id"], st and st.provider_call_id, etype,
             json.dumps({"duration_s": st and st.duration_s, "source": "reconcile"})))
        done.append((str(c["id"]), etype))
    return done


def release_lost_agent_calls(conn, providers: dict, worker_id: str, limit: int = 50) -> list[str]:
    """Agent paused / went offline during setup (agent_actions cleared reserved_call_id).
    Not answered yet: cancel at the provider. Already answered: the answer-hold loop hands the
    call to another agent or abandons it, so nothing to do here for ANSWERED."""
    rows = conn.execute(
        """SELECT c.id FROM calls c JOIN agents a ON a.id = c.agent_id
           WHERE c.mode = 'PROGRESSIVE' AND c.state IN ('RESERVED','INITIATED','RINGING')
             AND a.reserved_call_id IS DISTINCT FROM c.id
           LIMIT %s""", (limit,)).fetchall()
    cancelled = []
    for r in rows:
        with conn.transaction():
            verdict, call = advance_call(conn, r["id"], "CANCELLED", worker_id=worker_id, reason="AGENT_LOST")
        if call["state"] == "CANCELLED" and call["provider_call_id"]:
            providers[call["provider_name"]].hangup(call["provider_call_id"], "agent lost during setup")
        cancelled.append(str(r["id"]))
    return cancelled


def release_stuck_agents(conn, worker_id: str) -> int:
    """Backstop only: an agent still RESERVED/DIALING for a call that is already terminal. The
    transition code releases agents in the same transaction that ends the call, so this should
    never find anything; if it does, that is a bug worth a loud log line."""
    rows = conn.execute(
        """SELECT a.id, a.reserved_call_id FROM agents a JOIN calls c ON c.id = a.reserved_call_id
           WHERE a.state IN ('RESERVED','DIALING') AND c.state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED')""").fetchall()
    for r in rows:
        log.error("BUG: agent %s stuck on finished call %s, releasing", r["id"], r["reserved_call_id"])
        with conn.transaction():
            set_agent_state(conn, r["id"], "AVAILABLE", fence_call_id=r["reserved_call_id"], worker_id=worker_id,
                            reason="reaper backstop")
    return len(rows)


def run(conn, providers: dict, worker_id: str) -> dict:
    return {
        "reconciled": reconcile_calls(conn, providers, worker_id),
        "agent_lost": release_lost_agent_calls(conn, providers, worker_id),
        "stuck_agents": release_stuck_agents(conn, worker_id),
    }
