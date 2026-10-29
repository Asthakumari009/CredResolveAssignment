"""Applies the pure breaker to the shared provider_health row. Called by pacing leaders.

Provider health lives in the DB (not in each worker's memory) so every worker and the Safety
Controller see the same state. Outcomes are read from the calls table: each call row is one
dial attempt. Note for scale: one row per provider is a write hotspot (see docs/SCALING.md).
"""
from smartdialer.domain.breaker import next_state

_OUTCOMES = """
SELECT count(*) FILTER (WHERE ok) AS ok, count(*) FILTER (WHERE NOT ok) AS fail
FROM (SELECT (provider_call_id IS NOT NULL OR dialed_at IS NOT NULL) AS ok
      FROM calls
      WHERE provider_name = %(p)s AND created_at > %(since)s
        AND NOT (state = 'CANCELLED' AND failure_reason = 'AGENT_LOST')
        -- a dial still waiting for its response is neither success nor failure yet
        AND (provider_call_id IS NOT NULL OR dialed_at IS NOT NULL
             OR state = 'FAILED' AND failure_reason IN ('TIMEOUT','PROVIDER_ERROR')
             OR state IN ('QUEUED','RESERVED') AND created_at < now() - interval '3.5 seconds')) x
"""


def evaluate(conn, campaign_id: int) -> str:
    with conn.transaction():
        h = conn.execute(
            """SELECT h.*, EXTRACT(EPOCH FROM h.opened_at) AS since_s, EXTRACT(EPOCH FROM now()) AS now_s, now() AS now
               FROM provider_health h JOIN campaigns c ON c.provider_name = h.provider_name
               WHERE c.id = %s FOR UPDATE OF h""", (campaign_id,)).fetchone()
        if h is None:
            return "CLOSED"
        recent = conn.execute(_OUTCOMES, {"p": h["provider_name"], "since": _ago(conn, 30)}).fetchone()
        probe = (conn.execute(_OUTCOMES, {"p": h["provider_name"], "since": h["opened_at"]}).fetchone()
                 if h["breaker_state"] == "HALF_OPEN" else {"ok": 0, "fail": 0})
        new = next_state(h["breaker_state"], float(h["since_s"] or 0), float(h["now_s"]),
                         recent["ok"], recent["fail"], probe["ok"], probe["fail"])
        if new != h["breaker_state"]:
            conn.execute("UPDATE provider_health SET breaker_state = %s, opened_at = now(), updated_at = now() WHERE provider_name = %s",
                         (new, h["provider_name"]))
        return new


def _ago(conn, seconds: float):
    return conn.execute("SELECT now() - %s * interval '1 second' AS t", (seconds,)).fetchone()["t"]
