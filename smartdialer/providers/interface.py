"""What the dialer knows about a telecom provider. Nothing else about provider internals leaks in.

Three possible outcomes of a dial, and the dialer treats them differently:
  DialAccepted   the provider has the call; we record INITIATED.
  DialRejected   the provider definitely did not place the call; we record FAILED.
  DialUncertain  timeout / connection error: the call MAY exist. We record nothing and let
                 reconciliation ask get_call_status(idempotency_key) later. Never redial blindly.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DialAccepted:
    provider_call_id: str


@dataclass(frozen=True)
class DialRejected:
    reason: str


@dataclass(frozen=True)
class DialUncertain:
    reason: str


DialResult = DialAccepted | DialRejected | DialUncertain


@dataclass(frozen=True)
class CallStatus:
    status: str                    # NOT_FOUND, INITIATED, RINGING, ANSWERED, COMPLETED, NO_ANSWER, BUSY, FAILED
    provider_call_id: str | None = None
    duration_s: float | None = None


class ProviderUnavailable(Exception):
    """get_call_status could not get an answer (timeout, outage). Try again later."""


class Provider(Protocol):
    name: str

    def dial(self, idempotency_key: str, phone: str) -> DialResult: ...

    def hangup(self, provider_call_id: str, reason: str = "") -> None: ...

    def get_call_status(self, idempotency_key: str) -> CallStatus: ...
