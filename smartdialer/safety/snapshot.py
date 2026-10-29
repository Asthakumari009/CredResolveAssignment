"""One consistent read of everything pacing and safety need. Used independently by the pacing
engine and by the Safety Controller: the controller never trusts numbers the pacer passes in."""
from smartdialer.config import CampaignSettings
from smartdialer.domain.types import Snapshot

_TERMINAL = "('COMPLETED','FAILED','CANCELLED','ABANDONED')"

_SQL = f"""
SELECT now() AS taken_at,
       (now() AT TIME ZONE c.timezone)::time AS local_time,
       c.fencing_token,
       (c.pacing_lease_owner = %(worker)s AND c.pacing_lease_expires_at > now()) AS lease_mine,
       COALESCE((SELECT breaker_state FROM provider_health WHERE provider_name = c.provider_name), 'CLOSED') AS breaker_state,
       (SELECT EXTRACT(EPOCH FROM now() - opened_at) FROM provider_health WHERE provider_name = c.provider_name)
           AS breaker_age_s,
       a.*, k.*, w.*, ab.*, avgs.*,
       COALESCE((SELECT max((s.snapshot->>'staffed')::int) FROM safety_decisions s
                 WHERE s.campaign_id = c.id AND s.at > now() - %(drop_window)s * interval '1 second'), 0)
           AS max_staffed_recent
FROM campaigns c,
LATERAL (SELECT count(*) FILTER (WHERE state = 'AVAILABLE') AS available,
                count(*) FILTER (WHERE state NOT IN ('OFFLINE','PAUSED')) AS staffed,
                count(*) FILTER (WHERE state = 'WRAP_UP') AS wrap_up,
                count(*) FILTER (WHERE state IN ('RESERVED','DIALING','CONNECTED')) AS bound_agents
         FROM agents WHERE campaign_id = c.id) a,
LATERAL (SELECT count(*) FILTER (WHERE state IN ('QUEUED','RESERVED')) AS pre_dial,
                count(*) FILTER (WHERE state IN ('INITIATED','RINGING')) AS ringing,
                count(*) FILTER (WHERE agent_id IS NULL AND state IN ('QUEUED','INITIATED','RINGING')) AS unbound_in_flight,
                count(*) FILTER (WHERE state = 'ANSWERED') AS answered_waiting,
                count(*) FILTER (WHERE state = 'CONNECTED') AS connected
         FROM calls WHERE campaign_id = c.id AND state NOT IN {_TERMINAL}) k,
-- Answer-rate windows sample by DIAL time, and only from calls dialed before the oldest call
-- whose outcome is still unknown. Sampling by resolution time would be biased: answers resolve
-- in seconds, no-answers only after the full ring-out, so right after a burst of dials the most
-- recently resolved calls are mostly answers.
LATERAL (SELECT min(created_at) AS cutoff FROM calls
         WHERE campaign_id = c.id AND state IN ('QUEUED','RESERVED','INITIATED','RINGING')) u,
LATERAL (SELECT count(*) AS window_n, count(*) FILTER (WHERE ans) AS window_answered,
                count(*) FILTER (WHERE rn <= %(short)s) AS short_n,
                count(*) FILTER (WHERE rn <= %(short)s AND ans) AS short_answered
         FROM (SELECT (answered_at IS NOT NULL OR answered_inferred) AS ans,
                      row_number() OVER (ORDER BY created_at DESC) AS rn
               FROM calls
               WHERE campaign_id = c.id AND created_at < COALESCE(u.cutoff, 'infinity')
                 AND (answered_at IS NOT NULL OR answered_inferred
                      OR (state = 'FAILED' AND failure_reason IN ('NO_ANSWER','BUSY')))
               ORDER BY created_at DESC LIMIT %(window)s) x) w,
LATERAL (SELECT count(*) AS abandon_n, count(*) FILTER (WHERE connected_at IS NULL) AS abandon_count
         FROM (SELECT connected_at FROM calls
               WHERE campaign_id = c.id AND (answered_at IS NOT NULL OR answered_inferred)
                 AND (connected_at IS NOT NULL OR state IN {_TERMINAL})
               ORDER BY COALESCE(answered_at, ended_at) DESC LIMIT %(abandon_window)s) y) ab,
LATERAL (SELECT avg(EXTRACT(EPOCH FROM answered_at - dialed_at)) AS avg_setup_s,
                avg(duration_s) FILTER (WHERE connected_at IS NOT NULL) AS avg_talk_s
         FROM (SELECT answered_at, dialed_at, duration_s, connected_at FROM calls
               WHERE campaign_id = c.id AND state = 'COMPLETED'
               ORDER BY ended_at DESC LIMIT 200) z) avgs
WHERE c.id = %(campaign)s
"""


def load_snapshot(conn, campaign_id: int, cfg: CampaignSettings, worker_id: str, fencing_token: int | None) -> Snapshot:
    r = conn.execute(_SQL, {"campaign": campaign_id, "worker": worker_id, "short": cfg.short_window,
                            "window": cfg.answer_window, "abandon_window": cfg.abandon_window,
                            "drop_window": cfg.agent_drop_window_s}).fetchone()
    return Snapshot(
        campaign_id=campaign_id,
        taken_at=r["taken_at"],
        local_time=r["local_time"],
        leader_ok=bool(r["lease_mine"]) and fencing_token == r["fencing_token"],
        fencing_token=r["fencing_token"],
        breaker_state=r["breaker_state"],
        available=r["available"], staffed=r["staffed"], wrap_up=r["wrap_up"], bound_agents=r["bound_agents"],
        pre_dial=r["pre_dial"], ringing=r["ringing"], unbound_in_flight=r["unbound_in_flight"],
        answered_waiting=r["answered_waiting"], connected=r["connected"],
        window_n=r["window_n"], window_answered=r["window_answered"],
        short_n=r["short_n"], short_answered=r["short_answered"],
        abandon_n=r["abandon_n"], abandon_count=r["abandon_count"],
        avg_setup_s=float(r["avg_setup_s"]) if r["avg_setup_s"] is not None else None,
        avg_talk_s=float(r["avg_talk_s"]) if r["avg_talk_s"] is not None else None,
        max_staffed_recent=r["max_staffed_recent"],
        breaker_age_s=float(r["breaker_age_s"]) if r["breaker_age_s"] is not None else None,
    )
