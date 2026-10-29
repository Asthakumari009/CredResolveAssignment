def beat(conn, worker_id: str) -> None:
    """Worker liveness, for operators and demos. Correctness does not depend on it: recovery is
    driven by how long a call has been in an uncertain state, checked against the provider."""
    conn.execute(
        """INSERT INTO workers (id) VALUES (%s)
           ON CONFLICT (id) DO UPDATE SET last_heartbeat_at = now()""", (worker_id,))
