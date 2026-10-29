"""Safety Controller: the only component that can create dial permits.

A permit is a row in dial_permits: approved_count, consumed_count, a short expiry, and the
campaign fencing token. The allocator can only create a call by consuming one unit of the
current permit, in the same transaction that creates the call. There is no other path.
"""
import json

from smartdialer.config import load_settings
from smartdialer.domain import safety_rules
from smartdialer.domain.types import PacingProposal, SafetyDecision
from smartdialer.safety.snapshot import load_snapshot


class SafetyController:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    def decide(self, conn, campaign_id: int, fencing_token: int, proposal: PacingProposal) -> SafetyDecision:
        cfg = load_settings(conn, campaign_id)
        with conn.transaction():
            # LOCK ORDER: campaign -> permit. Holding the campaign row serializes deciders.
            # CONCURRENCY: FOR NO KEY UPDATE, not FOR UPDATE. Consumers hold the permit row and then
            # insert a call, whose foreign key check takes KEY SHARE on this campaign row. FOR UPDATE
            # blocks KEY SHARE, so decider (campaign, waiting on permit) and consumer (permit, waiting
            # on campaign) deadlocked under load. NO KEY UPDATE still excludes other deciders.
            camp = conn.execute("SELECT safety_state FROM campaigns WHERE id = %s FOR NO KEY UPDATE",
                                (campaign_id,)).fetchone()
            snap = load_snapshot(conn, campaign_id, cfg, self.worker_id, fencing_token)
            if snap.leader_ok:
                # Revoke first, then re-read: the revoke waits for any in-flight consume of the old
                # permit to commit, so the fresh snapshot below already counts those calls.
                conn.execute("UPDATE dial_permits SET revoked = true WHERE campaign_id = %s AND NOT revoked", (campaign_id,))
                snap = load_snapshot(conn, campaign_id, cfg, self.worker_id, fencing_token)
            decision, memory = safety_rules.decide(proposal, snap, cfg, camp["safety_state"])

            explanation = proposal.explanation + (" | " + "; ".join(decision.notes) if decision.notes else "")
            decision_id = conn.execute(
                """INSERT INTO safety_decisions (campaign_id, worker_id, fencing_token, requested, proposal_mode,
                       explanation, snapshot, approved_count, outcome, mode, reason_codes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (campaign_id, self.worker_id, fencing_token, proposal.requested_calls, proposal.mode, explanation,
                 json.dumps(snap.to_json()), decision.approved_calls, decision.outcome, decision.mode,
                 list(decision.reason_codes))).fetchone()["id"]
            permit_id = None
            if snap.leader_ok:
                conn.execute("UPDATE campaigns SET safety_state = %s WHERE id = %s", (json.dumps(memory), campaign_id))
                if decision.approved_calls > 0:
                    permit_id = conn.execute(
                        """INSERT INTO dial_permits (campaign_id, fencing_token, mode, approved_count, expires_at, decision_id)
                           VALUES (%s, %s, %s, %s, now() + %s * interval '1 millisecond', %s) RETURNING id""",
                        (campaign_id, fencing_token, decision.mode, decision.approved_calls, cfg.permit_ttl_ms,
                         decision_id)).fetchone()["id"]
        return SafetyDecision(decision.approved_calls, decision.mode, decision.outcome, decision.reason_codes,
                              permit_id, decision_id, decision.notes)

    @staticmethod
    def consume_one(conn, campaign_id: int):
        """Take one unit of the campaign's live permit. Must run inside the caller's transaction,
        the same one that creates the call, so a rollback gives the unit back.

        # CONCURRENCY: many workers consume the same permit row. The UPDATE takes the row lock;
        # a concurrent consumer waits, then Postgres re-evaluates the WHERE clause against the
        # updated row, so `consumed_count < approved_count` is checked on the latest value and
        # the permit can never be over-consumed (and the CHECK constraint backs this up).
        # revoked, expires_at and the fencing token are checked in the same statement, so a
        # revoked, expired, or deposed-leader permit yields 0 rows.
        """
        return conn.execute(
            """UPDATE dial_permits p SET consumed_count = p.consumed_count + 1
               FROM campaigns c
               WHERE p.campaign_id = %s AND c.id = p.campaign_id
                 AND p.revoked = false
                 AND p.consumed_count < p.approved_count
                 AND p.expires_at > now()
                 AND p.fencing_token = c.fencing_token
               RETURNING p.id, p.mode""", (campaign_id,)).fetchone()
