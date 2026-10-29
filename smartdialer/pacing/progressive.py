from smartdialer.domain.types import PacingProposal, Snapshot


def propose(snap: Snapshot) -> PacingProposal:
    """One call per AVAILABLE agent. The real bound is the atomic agent reservation at dispatch."""
    return PacingProposal(
        requested_calls=snap.available,
        mode="PROGRESSIVE",
        explanation=f"progressive: available={snap.available} -> proposing {snap.available}",
        snapshot_taken_at=snap.taken_at,
    )
