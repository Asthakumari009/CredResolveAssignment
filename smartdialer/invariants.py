"""Invariant checks over the history tables. Used by every demo, the load test and tests.

Each check returns a count of violations; zero means it holds. The history tables are
append-only, so these prove properties over the whole run, not just the final state.
"""
from smartdialer.domain import agent_sm, call_sm

_TERMINAL = "('COMPLETED','FAILED','CANCELLED','ABANDONED')"


def _count(conn, sql, params=None) -> int:
    return conn.execute(sql, params).fetchone()["n"]


def check_all(conn, settle_checks: bool = True) -> list[tuple[str, int]]:
    """settle_checks: also check end-state properties (no stuck agents / borrowers). Only valid
    once the system is quiet (all calls terminal or truly in progress)."""
    results = [
        ("no borrower ever had two overlapping calls", _count(conn, f"""
            SELECT count(*) AS n FROM calls a JOIN calls b ON a.borrower_id = b.borrower_id AND a.id < b.id
            WHERE a.created_at < COALESCE(b.ended_at, 'infinity') AND b.created_at < COALESCE(a.ended_at, 'infinity')""")),
        ("no agent ever bound to two calls at once (transition chain)", _agent_chain_violations(conn)),
        ("every call transition is allowed and chained", _call_chain_violations(conn)),
        ("no duplicate call transitions", _count(conn, """
            SELECT count(*) AS n FROM (SELECT call_id, to_state FROM call_transitions
                                       GROUP BY 1, 2 HAVING count(*) > 1) d""")),
        ("permits never over-consumed", _count(conn, "SELECT count(*) AS n FROM dial_permits WHERE consumed_count > approved_count")),
        ("calls per permit == consumed units", _count(conn, """
            SELECT count(*) AS n FROM dial_permits p
            WHERE p.consumed_count <> (SELECT count(*) FROM calls c WHERE c.permit_id = p.id)""")),
        ("every call was created against a permit", _count(conn, "SELECT count(*) AS n FROM calls WHERE permit_id IS NULL")),
    ]
    if settle_checks:
        results += [
            ("no agent stuck bound to a finished or missing call", _count(conn, f"""
                SELECT count(*) AS n FROM agents a LEFT JOIN calls c ON c.id = a.reserved_call_id
                WHERE a.state IN ('RESERVED','DIALING','CONNECTED')
                  AND (c.id IS NULL OR c.state IN {_TERMINAL})""")),
            ("no borrower IN_PROGRESS without a live call", _count(conn, f"""
                SELECT count(*) AS n FROM borrowers b WHERE b.state = 'IN_PROGRESS'
                  AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.borrower_id = b.id AND c.state NOT IN {_TERMINAL})""")),
        ]
    return results


def _agent_chain_violations(conn) -> int:
    """Each agent transition must start where the previous one ended, be allowed by the state
    machine, and acquire a call only from AVAILABLE. Together these mean an agent can never be
    bound to a second call while bound to the first."""
    bad = 0
    prev: dict[int, str] = {}
    for r in conn.execute("SELECT agent_id, from_state, to_state FROM agent_transitions ORDER BY id"):
        p = prev.get(r["agent_id"])
        if (p is not None and p != r["from_state"]) or not agent_sm.can_transition(r["from_state"], r["to_state"]):
            bad += 1
        prev[r["agent_id"]] = r["to_state"]
    return bad


def _call_chain_violations(conn) -> int:
    bad = 0
    prev: dict = {}
    for r in conn.execute("SELECT call_id, from_state, to_state FROM call_transitions ORDER BY id"):
        p = prev.get(r["call_id"], None)
        if r["from_state"] is None:
            ok = r["call_id"] not in prev
        else:
            ok = p == r["from_state"] and call_sm.classify(r["from_state"], r["to_state"]) == call_sm.Verdict.APPLY
        bad += not ok
        prev[r["call_id"]] = r["to_state"]
    return bad


def report(conn, settle_checks: bool = True) -> bool:
    results = check_all(conn, settle_checks)
    print("\nInvariant checks")
    for name, n in results:
        print(f"  [{'PASS' if n == 0 else 'FAIL'}] {name}" + (f"  ({n} violations)" if n else ""))
    return all(n == 0 for _, n in results)
