-- Reconciliation bookkeeping, and an index for per-borrower history (invariant checks).
ALTER TABLE calls ADD COLUMN last_reconciled_at timestamptz;
CREATE INDEX calls_borrower ON calls (borrower_id, created_at);
CREATE INDEX calls_pending_reconcile ON calls (state_changed_at)
    WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED');
