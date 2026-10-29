def try_lead(conn, campaign_id: int, worker_id: str, lease_ms: int) -> int | None:
    """Acquire or renew the campaign pacing lease. Returns the fencing token, or None if another
    worker holds a live lease.

    # CONCURRENCY: the WHERE clause only matches if the lease is ours or has expired, and the
    # UPDATE row lock makes concurrent attempts serialize: after the first takeover commits, the
    # second attempt re-checks the WHERE on the new row (owner is now someone else, lease live)
    # and matches 0 rows. A takeover increments fencing_token, which instantly invalidates every
    # permit the previous leader issued (permits are only consumable with the current token).
    """
    row = conn.execute(
        """UPDATE campaigns
           SET fencing_token = CASE WHEN pacing_lease_owner = %(w)s THEN fencing_token ELSE fencing_token + 1 END,
               pacing_lease_owner = %(w)s,
               pacing_lease_expires_at = now() + %(ms)s * interval '1 millisecond'
           WHERE id = %(c)s
             AND (pacing_lease_owner = %(w)s OR pacing_lease_owner IS NULL OR pacing_lease_expires_at < now())
           RETURNING fencing_token""", {"w": worker_id, "ms": lease_ms, "c": campaign_id}).fetchone()
    return row["fencing_token"] if row else None
