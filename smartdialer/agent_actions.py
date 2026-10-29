"""Actions taken by the human agent (log in, pause, resume, go offline, finish wrap-up).

These lock only the agent row. Pausing or going offline clears reserved_call_id, so every
worker still holding that call id is fenced out from then on. The call left without its agent
is cleaned up separately by a worker (reaper: cancel if not answered, hand off or abandon if
answered), in its own transaction in the normal lock order.
"""
from smartdialer.domain.agent_sm import can_transition
from smartdialer.transitions import log_agent


def agent_action(conn, agent_id: int, to: str, reason: str = "agent action") -> bool:
    with conn.transaction():
        a = conn.execute("SELECT state, reserved_call_id FROM agents WHERE id = %s FOR UPDATE", (agent_id,)).fetchone()
        if a is None or not can_transition(a["state"], to):
            return False
        release = to in ("AVAILABLE", "PAUSED", "OFFLINE")
        conn.execute(
            """UPDATE agents SET state = %(to)s, state_changed_at = clock_timestamp(), last_heartbeat_at = now(),
                   reserved_call_id   = CASE WHEN %(release)s THEN NULL ELSE reserved_call_id END,
                   reserved_by_worker = CASE WHEN %(release)s THEN NULL ELSE reserved_by_worker END
               WHERE id = %(id)s""", {"to": to, "release": release, "id": agent_id})
        log_agent(conn, agent_id, a["state"], to, a["reserved_call_id"], None, reason)
    return True


def login_all(conn, campaign_id: int) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM agents WHERE campaign_id = %s AND state = 'OFFLINE' ORDER BY id", (campaign_id,))]
    return sum(agent_action(conn, i, "AVAILABLE", "login") for i in ids)
