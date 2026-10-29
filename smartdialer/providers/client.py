"""HTTP client for a provider. Maps transport outcomes onto the three dial outcomes the dialer
understands; the dialer never sees provider-specific behaviour."""
import logging

import httpx

from smartdialer.providers.interface import CallStatus, DialAccepted, DialRejected, DialUncertain, ProviderUnavailable

log = logging.getLogger(__name__)


class HttpProvider:
    def __init__(self, name: str, base_url: str, timeout_s: float = 3.0):
        self.name = name
        self.http = httpx.Client(base_url=f"{base_url.rstrip('/')}/{name.lower()}", timeout=timeout_s)

    def dial(self, idempotency_key: str, phone: str):
        try:
            r = self.http.post("/dial", json={"idempotency_key": idempotency_key, "phone": phone})
        except httpx.TransportError as e:   # timeout, connection refused/reset: the call MAY exist
            return DialUncertain(type(e).__name__)
        if r.status_code == 200:
            return DialAccepted(r.json()["provider_call_id"])
        if r.status_code >= 500 and r.status_code != 503:
            return DialUncertain(f"http {r.status_code}")   # server blew up mid-request: unknown
        return DialRejected(f"http {r.status_code}")         # explicit refusal: not placed

    def hangup(self, provider_call_id: str, reason: str = "") -> None:
        try:
            self.http.post("/hangup", json={"provider_call_id": provider_call_id, "reason": reason})
        except httpx.TransportError as e:
            log.warning("hangup %s failed: %s (reconciliation will see the call end)", provider_call_id, e)

    def get_call_status(self, idempotency_key: str) -> CallStatus:
        try:
            r = self.http.get(f"/calls/{idempotency_key}")
        except httpx.TransportError as e:
            raise ProviderUnavailable(type(e).__name__) from e
        if r.status_code == 404:
            return CallStatus("NOT_FOUND")
        if r.status_code != 200:
            raise ProviderUnavailable(f"http {r.status_code}")
        d = r.json()
        return CallStatus(d["status"], d.get("provider_call_id"), d.get("duration_s"))
