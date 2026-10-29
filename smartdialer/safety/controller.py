"""Safety Controller: the only component that can create dial permits.

A permit is a row in dial_permits: approved_count, consumed_count (units CLAIMED by workers),
a short expiry, and the campaign fencing token. Workers claim units in small batches with one
short statement (claim_units), then spend them one call at a time. Each call transaction
first pins the permit (pin) and only creates the call if the permit is still live.

Claimed units that never become calls are dropped, never returned: calls <= claimed <= approved.
Dropping can only mean fewer calls than approved, never more (ADR-4).
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
        # Step 1, its own short transaction: retire the live permit, so from now on no call
        # transaction can pin it. Leader-only (same test as the snapshot's leader_ok): a
        # deposed leader or an impostor cannot retire the real leader's permit.
        retired = [r["id"] for r in conn.execute(
            """UPDATE dial_permits p SET revoked = true FROM campaigns c
               WHERE p.campaign_id = %(c)s AND c.id = p.campaign_id AND NOT p.revoked
                 AND c.fencing_token = %(t)s
                 AND c.pacing_lease_owner = %(w)s AND c.pacing_lease_expires_at > now()
               RETURNING p.id""", {"c": campaign_id, "t": fencing_token, "w": self.worker_id})]
        with conn.transaction():
            # LOCK ORDER: campaign -> permit. Holding the campaign row serializes deciders.
            # CONCURRENCY: FOR NO KEY UPDATE, not FOR UPDATE. Call transactions hold the permit and
            # then insert a call, whose foreign key check takes KEY SHARE on this campaign row, while
            # we wait on that permit below. FOR UPDATE blocks KEY SHARE: that deadlocked under load
            # (PLAN D10). NO KEY UPDATE allows KEY SHARE and still excludes other deciders.
            camp = conn.execute("SELECT safety_state FROM campaigns WHERE id = %s FOR NO KEY UPDATE",
                                (campaign_id,)).fetchone()
            # CONCURRENCY: call transactions hold the permit FOR KEY SHARE (see pin) from their
            # validity check until commit. FOR UPDATE waits for exactly those that pinned before
            # step 1 committed; none can pin after it. So the snapshot below counts every call
            # the retired permit will ever produce. The waiter set is finite: no starvation.
            if retired:
                conn.execute("SELECT id FROM dial_permits WHERE id = ANY(%s) FOR UPDATE", (retired,))
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
    def claim_units(conn, campaign_id: int, want: int):
        """Claim up to `want` units of the campaign's live permit. Returns
        {"id", "mode", "units"} or None. Run OUTSIDE any transaction: each attempt is one
        autocommit statement, so the permit row lock is held for that statement only, never
        while a borrower or agent is being reserved.

        # CONCURRENCY: many workers claim from the same row. The UPDATE takes the row lock; a
        # concurrent claimer waits, then Postgres re-evaluates the WHERE clause on the updated
        # row, so `consumed_count + k <= approved_count` is checked on the latest value. Units
        # are never over-claimed (and the CHECK constraint backs this up). revoked, expires_at
        # and the fencing token are checked in the same statement. If k does not fit, try a
        # smaller k, down to 1.
        """
        k = max(1, want)
        while k >= 1:
            row = conn.execute(
                """UPDATE dial_permits p SET consumed_count = p.consumed_count + %(k)s
                   FROM campaigns c
                   WHERE p.campaign_id = %(c)s AND c.id = p.campaign_id
                     AND p.revoked = false
                     AND p.expires_at > now()
                     AND p.fencing_token = c.fencing_token
                     AND p.consumed_count + %(k)s <= p.approved_count
                   RETURNING p.id, p.mode""", {"c": campaign_id, "k": k}).fetchone()
            if row:
                return {**row, "units": k}
            k //= 2
        return None

    @staticmethod
    def pin(conn, permit_id: int):
        """Inside the call transaction, before anything else: is this permit still live? If so,
        hold it FOR KEY SHARE until commit, so the next decision waits for this call (see decide).

        # CONCURRENCY: KEY SHARE does not conflict with other pins or with claim_units (a
        # non-key UPDATE), so pins do not contend. It conflicts only with the decider's FOR UPDATE.
        """
        return conn.execute(
            """SELECT p.id, p.mode FROM dial_permits p JOIN campaigns c ON c.id = p.campaign_id
               WHERE p.id = %s AND p.revoked = false AND p.expires_at > now()
                 AND p.fencing_token = c.fencing_token
               FOR KEY SHARE OF p""", (permit_id,)).fetchone()
