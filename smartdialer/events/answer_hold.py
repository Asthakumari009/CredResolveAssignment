"""Answered calls waiting for an agent (predictive overflow, or a progressive agent who left).

Oldest answer first: claim an AVAILABLE agent; if none and the call has waited longer than
max_answer_hold_ms, ABANDON it, play the polite recorded message and hang up. Every abandon is
a row in calls (state ABANDONED) and is counted by the Safety Controller's ABANDON_BUDGET rule.
"""
from smartdialer.config import CampaignSettings
from smartdialer.transitions import advance_call, try_connect

D = CampaignSettings()


def run(conn, providers: dict, worker_id: str, limit: int = 50) -> dict:
    connected, abandoned = 0, []
    ids = conn.execute(
        "SELECT id FROM calls WHERE state = 'ANSWERED' ORDER BY answered_at LIMIT %s", (limit,)).fetchall()
    for r in ids:
        with conn.transaction():
            # LOCK ORDER: call -> (borrower) -> agent. SKIP LOCKED: if the event processor holds this
            # call right now, leave it to them and look again on the next pass.
            call = conn.execute(
                """SELECT c.*, EXTRACT(EPOCH FROM now() - c.answered_at) * 1000 AS waited_ms,
                          COALESCE((k.settings->>'max_answer_hold_ms')::int, %s) AS max_hold_ms
                   FROM calls c JOIN campaigns k ON k.id = c.campaign_id
                   WHERE c.id = %s AND c.state = 'ANSWERED' FOR UPDATE OF c SKIP LOCKED""",
                (D.max_answer_hold_ms, r["id"])).fetchone()
            if call is None:
                continue
            after = try_connect(conn, call, worker_id)
            if after["state"] == "CONNECTED":
                connected += 1
            elif call["waited_ms"] > call["max_hold_ms"]:
                _, after = advance_call(conn, call["id"], "ABANDONED", worker_id=worker_id, reason="NO_AGENT_IN_TIME")
                abandoned.append(after)
    for call in abandoned:   # network I/O only after commit
        if call["provider_call_id"]:
            providers[call["provider_name"]].hangup(call["provider_call_id"], "abandon: polite recorded message played")
    return {"connected": connected, "abandoned": len(abandoned)}
