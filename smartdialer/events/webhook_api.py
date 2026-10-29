"""Webhook receiver: provider events go into the provider_events inbox, nothing else.

It does not apply events and does not care which worker placed the call. Any worker's event
processor picks the row up later. That is what makes worker crashes survivable: an event is
never "for" a particular process.

Response is 200 for every well-formed event, including duplicates, so the provider stops
retrying. Only a DB failure returns 5xx (and the provider retries, which is what we want).
"""
import json
import os
import uuid

from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

app = FastAPI(title="smartdialer webhook")
pool = ConnectionPool(os.environ.get("DATABASE_URL", ""), min_size=2, max_size=10, open=False,
                      kwargs={"autocommit": True, "row_factory": dict_row})


@app.on_event("startup")
def _open():
    pool.open(wait=True)


class Event(BaseModel):
    event_id: str
    type: str
    call_id: str | None = None
    provider_call_id: str | None = None
    seq: int | None = None
    ts: str | None = None
    duration_s: float | None = None


def _uuid_or_none(v):
    try:
        return uuid.UUID(v) if v else None
    except ValueError:
        return None   # garbage key: stored anyway, ends up ORPHAN


@app.post("/events/{provider}")
def receive(provider: str, ev: Event):
    with pool.connection() as conn:
        row = conn.execute(
            """INSERT INTO provider_events (provider_name, provider_event_id, call_id, provider_call_id, event_type,
                   sequence, provider_timestamp, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               -- First duplicate barrier: an exact redelivery (same provider event id) is dropped here.
               ON CONFLICT (provider_name, provider_event_id) DO NOTHING
               RETURNING id""",
            (provider.upper(), ev.event_id, _uuid_or_none(ev.call_id), ev.provider_call_id, ev.type.upper(),
             ev.seq, ev.ts, json.dumps(ev.model_dump()))).fetchone()
    return {"stored": row is not None, "duplicate": row is None}


@app.get("/health")
def health():
    return {"ok": True}
