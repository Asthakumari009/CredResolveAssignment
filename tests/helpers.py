import itertools
import json
import uuid

from smartdialer.providers.interface import CallStatus, DialAccepted

ALWAYS_OPEN = {"window_start": "00:00", "window_end": "00:00"}


def make_campaign(conn, *, mode="PROGRESSIVE", agents=0, borrowers=0, provider="A", settings=None, agent_state="AVAILABLE"):
    s = {**ALWAYS_OPEN, **(settings or {})}
    cid = conn.execute("INSERT INTO campaigns (name, mode, provider_name, settings) VALUES ('test', %s, %s, %s) RETURNING id",
                       (mode, provider, json.dumps(s))).fetchone()["id"]
    if agents:
        conn.execute("INSERT INTO agents (campaign_id, name, state) SELECT %s, 'agent-' || g, %s FROM generate_series(1, %s) g",
                     (cid, agent_state, agents))
    if borrowers:
        conn.execute("INSERT INTO borrowers (campaign_id, phone) SELECT %s, '+9190000' || g FROM generate_series(1, %s) g",
                     (cid, borrowers))
    return cid


def issue_permit(conn, cid, approved, *, mode="PROGRESSIVE", ttl_ms=60_000, token=None):
    """Test shortcut: a permit without going through pacing (the controller is tested separately)."""
    if token is None:
        token = conn.execute("SELECT fencing_token FROM campaigns WHERE id = %s", (cid,)).fetchone()["fencing_token"]
    conn.execute("UPDATE dial_permits SET revoked = true WHERE campaign_id = %s AND NOT revoked", (cid,))
    return conn.execute(
        "INSERT INTO dial_permits (campaign_id, fencing_token, mode, approved_count, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + %s * interval '1 millisecond') RETURNING id",
        (cid, token, mode, approved, ttl_ms)).fetchone()["id"]


class FakeProvider:
    """In-process provider: idempotent on the key, records what it was asked."""
    name = "A"

    def __init__(self, result_for=None):
        self.calls: dict[str, str] = {}
        self.hangups: list[str] = []
        self.result_for = result_for   # optional fn(key) -> DialResult
        self._ids = itertools.count(1)

    def dial(self, key, phone):
        if self.result_for:
            r = self.result_for(key)
            if not isinstance(r, DialAccepted):
                return r
        if key not in self.calls:
            self.calls[key] = f"pc-{next(self._ids)}-{uuid.uuid4().hex[:6]}"
        return DialAccepted(self.calls[key])

    def hangup(self, provider_call_id, reason=""):
        self.hangups.append(provider_call_id)

    def get_call_status(self, key):
        if key in self.calls:
            return CallStatus("INITIATED", self.calls[key])
        return CallStatus("NOT_FOUND")
