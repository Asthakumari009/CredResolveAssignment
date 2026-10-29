"""Mock telecom providers A and B in one process: same HTTP API, different behaviour.

    POST /{p}/dial            {idempotency_key, phone} -> {provider_call_id}. Idempotent on the key.
    POST /{p}/hangup          {provider_call_id, reason}
    GET  /{p}/calls/{key}     -> {status, provider_call_id, duration_s} or 404
    POST /{p}/control         change profile/scenario at runtime (answer rate, outage, ...)
    POST /control/reset       forget all calls

Each call's fate (answered or not, talk time) is drawn at dial time from the scenario. Events
are POSTed to the webhook (WEBHOOK_URL/{p}). Provider B misbehaves on purpose: slow, timeouts
(the call IS placed but the response arrives after the client gave up), duplicate events,
reordering, and dropped intermediate events. All durations are multiplied by time_scale so a
120 s call can run in a couple of real seconds; reported durations are the unscaled ones.
"""
import asyncio
import os
import random
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:8002/events")


@dataclass
class Profile:
    latency_ms: tuple[int, int]
    error_rate: float            # explicit 503: call NOT placed
    timeout_rate: float          # call placed, response too late
    duplicate_rate: float
    reorder_rate: float
    drop_rate: float             # drop RINGING / ANSWERED (never the final event)
    answer_rate: float = 0.3
    busy_rate: float = 0.05
    ring_s: tuple[float, float] = (2.0, 8.0)
    talk_mean_s: float = 120.0
    time_scale: float = 0.02
    outage: bool = False
    auto_events: bool = True     # False: accept dials but send no events (demos craft them)
    seed: int | None = None


PROFILES = {
    "a": Profile(latency_ms=(50, 150), error_rate=0.01, timeout_rate=0.0, duplicate_rate=0.0, reorder_rate=0.0, drop_rate=0.0),
    "b": Profile(latency_ms=(300, 2000), error_rate=0.0, timeout_rate=0.08, duplicate_rate=0.15, reorder_rate=0.20, drop_rate=0.05),
}
DEFAULTS = {k: replace(v) for k, v in PROFILES.items()}


@dataclass
class MockCall:
    key: str
    provider_call_id: str
    phone: str
    status: str = "INITIATED"
    answered_at: float | None = None
    duration_s: float | None = None
    seq: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)


CALLS: dict[str, dict[str, MockCall]] = {"a": {}, "b": {}}
BY_PCID: dict[str, MockCall] = {}
RNG = random.Random()
app = FastAPI(title="mock providers")
http = httpx.AsyncClient(timeout=5.0)


class Dial(BaseModel):
    idempotency_key: str
    phone: str


class Hangup(BaseModel):
    provider_call_id: str
    reason: str = ""


def _prof(p: str) -> Profile:
    if p not in PROFILES:
        raise HTTPException(404, "unknown provider")
    return PROFILES[p]


async def _outage_gate(prof: Profile):
    if prof.outage:
        await asyncio.sleep(30)   # far beyond any client timeout
        raise HTTPException(504)


@app.post("/{p}/dial")
async def dial(p: str, body: Dial):
    prof = _prof(p)
    await _outage_gate(prof)
    await asyncio.sleep(RNG.uniform(*prof.latency_ms) / 1000)
    existing = CALLS[p].get(body.idempotency_key)
    if existing:
        return {"provider_call_id": existing.provider_call_id, "duplicate_dial": True}
    if RNG.random() < prof.error_rate:
        raise HTTPException(503, "provider error, call not placed")
    call = MockCall(body.idempotency_key, f"{p}-{uuid.uuid4().hex[:12]}", body.phone)
    CALLS[p][call.key] = call
    BY_PCID[call.provider_call_id] = call
    if prof.auto_events:
        call.task = asyncio.create_task(_lifecycle(p, prof, call))
    if RNG.random() < prof.timeout_rate:
        await asyncio.sleep(10)   # placed, but the client times out before seeing this
    return {"provider_call_id": call.provider_call_id}


@app.post("/{p}/hangup")
async def hangup(p: str, body: Hangup):
    prof = _prof(p)
    await _outage_gate(prof)
    call = BY_PCID.get(body.provider_call_id)
    if call is None:
        raise HTTPException(404)
    if call.status in ("COMPLETED", "NO_ANSWER", "BUSY", "FAILED"):
        return {"status": call.status}
    if call.task:
        call.task.cancel()
    talked = (asyncio.get_running_loop().time() - call.answered_at) / prof.time_scale if call.answered_at else 0.0
    await _finish(p, prof, call, "COMPLETED", round(talked, 1))
    return {"status": call.status}


@app.get("/{p}/calls/{key}")
async def status(p: str, key: str):
    prof = _prof(p)
    await _outage_gate(prof)
    call = CALLS[p].get(key)
    if call is None:
        raise HTTPException(404)
    return {"status": call.status, "provider_call_id": call.provider_call_id, "duration_s": call.duration_s}


@app.post("/{p}/control")
async def control(p: str, changes: dict):
    prof = _prof(p)
    for k, v in changes.items():
        if not hasattr(prof, k):
            raise HTTPException(400, f"unknown setting {k}")
        setattr(prof, k, tuple(v) if isinstance(v, list) else v)
    if "seed" in changes and changes["seed"] is not None:
        RNG.seed(changes["seed"])
    return prof.__dict__


@app.post("/control/reset")
async def reset():
    for p in CALLS:
        for c in CALLS[p].values():
            if c.task:
                c.task.cancel()
        CALLS[p].clear()
        PROFILES[p] = replace(DEFAULTS[p])
    BY_PCID.clear()
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


async def _lifecycle(p: str, prof: Profile, call: MockCall):
    """The provider's truth for this call. Status changes immediately; events are sent separately
    and may be delayed, duplicated or dropped."""
    s = prof.time_scale
    await asyncio.sleep(RNG.uniform(0.3, 1.0) * s * 10)
    await _event(p, prof, call, "RINGING")
    ring = RNG.uniform(*prof.ring_s)
    r = RNG.random()
    if r < prof.answer_rate:
        await asyncio.sleep(ring * s)
        call.answered_at = asyncio.get_running_loop().time()
        await _event(p, prof, call, "ANSWERED")
        talk = RNG.expovariate(1 / prof.talk_mean_s)
        await asyncio.sleep(talk * s)
        await _finish(p, prof, call, "COMPLETED", round(talk, 1))
    elif r < prof.answer_rate + prof.busy_rate:
        await asyncio.sleep(0.5 * s)
        await _finish(p, prof, call, "BUSY", 0.0)
    else:
        await asyncio.sleep(30 * s)   # ring out
        await _finish(p, prof, call, "NO_ANSWER", 0.0)


async def _finish(p, prof, call, status, duration):
    call.duration_s = duration
    await _event(p, prof, call, status, final=True)


async def _event(p: str, prof: Profile, call: MockCall, etype: str, final: bool = False):
    call.status = etype
    call.seq += 1
    if not final and RNG.random() < prof.drop_rate:
        return   # lost forever
    payload = {
        "event_id": uuid.uuid4().hex, "call_id": call.key, "provider_call_id": call.provider_call_id,
        "type": etype, "seq": call.seq, "ts": datetime.now(timezone.utc).isoformat(),
        "duration_s": call.duration_s if final else None,
    }
    delay = RNG.uniform(0.5, 3.0) * prof.time_scale * 50 if RNG.random() < prof.reorder_rate else 0.0
    asyncio.create_task(_deliver(p, payload, delay))
    if RNG.random() < prof.duplicate_rate:
        dup = payload if RNG.random() < 0.7 else {**payload, "event_id": uuid.uuid4().hex}   # same id, or a new id
        asyncio.create_task(_deliver(p, dup, delay + RNG.uniform(0.05, 1.0)))


async def _deliver(p: str, payload: dict, delay: float):
    if delay:
        await asyncio.sleep(delay)
    for attempt in range(5):   # at-least-once delivery, like real providers
        try:
            r = await http.post(f"{WEBHOOK_URL}/{p.upper()}", json=payload)
            if r.status_code < 500:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2 * 2 ** attempt)
