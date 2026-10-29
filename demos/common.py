"""Shared plumbing for the failure demos: a fresh database, the mock provider and webhook
running as real processes, and a readable timeline."""
import os
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from smartdialer.db import connect
from smartdialer.migrate import migrate

MOCK_URL = "http://127.0.0.1:8001"
WEBHOOK_URL = "http://127.0.0.1:8002"
TABLES = ("provider_events", "call_transitions", "agent_transitions", "safety_decisions", "calls",
          "dial_permits", "borrowers", "agents", "workers", "campaigns")
T0 = time.monotonic()


def say(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.2f}s] {msg}", flush=True)


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78, flush=True)


def fresh_db():
    migrate()
    conn = connect()
    conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    conn.execute("UPDATE provider_health SET breaker_state = 'CLOSED', opened_at = NULL")
    return conn


def _wait_healthy(url: str, proc: subprocess.Popen):
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited with {proc.returncode}")
        try:
            if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


@contextmanager
def services():
    """Mock provider (8001) and webhook receiver (8002) as separate processes."""
    env = {**os.environ, "WEBHOOK_URL": WEBHOOK_URL + "/events"}
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_provider.app:app", "--port", "8001", "--log-level", "warning"], env=env),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "smartdialer.events.webhook_api:app", "--port", "8002",
                          "--log-level", "warning", "--workers", "1"], env=env),
    ]
    try:
        _wait_healthy(MOCK_URL, procs[0])
        _wait_healthy(WEBHOOK_URL, procs[1])
        httpx.post(MOCK_URL + "/control/reset")
        yield
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)


def control(provider: str, **changes):
    httpx.post(f"{MOCK_URL}/{provider.lower()}/control", json=changes).raise_for_status()


def post_event(provider: str, call_id, etype: str, event_id: str | None = None, duration_s: float | None = None) -> dict:
    import uuid
    body = {"event_id": event_id or uuid.uuid4().hex, "type": etype, "call_id": str(call_id), "duration_s": duration_s}
    return httpx.post(f"{WEBHOOK_URL}/events/{provider}", json=body).json()


def print_call(conn, call_id) -> None:
    print("\n  call_transitions:")
    for r in conn.execute("SELECT from_state, to_state, cause_event_id, worker_id, at FROM call_transitions"
                          " WHERE call_id = %s ORDER BY id", (call_id,)):
        print(f"    {r['at']:%H:%M:%S.%f}  {str(r['from_state']):>10} -> {r['to_state']:<10} event={r['cause_event_id']} worker={r['worker_id']}")
    print("  provider_events inbox:")
    for r in conn.execute("SELECT id, provider_event_id, event_type, processing_result FROM provider_events"
                          " WHERE call_id = %s ORDER BY id", (call_id,)):
        print(f"    #{r['id']:<4} {r['event_type']:<10} id={r['provider_event_id'][:12]:<12} -> {r['processing_result']}")


def finish(conn, ok: bool = True) -> None:
    from smartdialer.invariants import report
    ok = report(conn) and ok
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
