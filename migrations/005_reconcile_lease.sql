-- Reconciliation lease: one reaper owns a call's status poll until it writes the result or the lease runs out.
ALTER TABLE calls ADD COLUMN reconcile_owner text, ADD COLUMN reconcile_lease_until timestamptz;
