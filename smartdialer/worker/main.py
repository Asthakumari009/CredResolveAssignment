"""Dialer worker: one OS process, one thin loop. Run N of them; any can crash at any time.

Each pass:
    heartbeat (1 s)                     -> workers row, for operators
    per campaign, every pacing tick:    -> only the pacing leader: pace -> Safety Controller -> permit
    per campaign:                       -> dispatch calls against the live permit (any worker)
    process inbox events                -> any worker
    answer-hold                         -> connect or abandon answered calls waiting for an agent
    reaper (1 s)                        -> reconcile uncertain calls with the provider

The loop only sequences calls into the modules; the decisions live in pure functions
(domain/, pacing/) that the simulator also uses. Keep it that way: logic added here would
drift away from what the simulator measures.
"""
import argparse
import json
import logging
import os
import signal
import time
import traceback
import uuid

from smartdialer.allocator.allocator import CallAllocator
from smartdialer.config import CampaignSettings
from smartdialer.db import connect
from smartdialer.events import answer_hold
from smartdialer.events.processor import process_batch
from smartdialer.pacing import predictive, progressive
from smartdialer.providers.client import HttpProvider
from smartdialer.recovery import reaper
from smartdialer.recovery.heartbeat import beat
from smartdialer.safety import breaker_store
from smartdialer.safety.controller import SafetyController
from smartdialer.safety.leader import try_lead
from smartdialer.safety.snapshot import load_snapshot

log = logging.getLogger("worker")
DISPATCH_PER_PASS = 20


class Worker:
    def __init__(self, worker_id: str, provider_url: str, dial_timeout_s: float = 3.0):
        self.id = worker_id
        self.conn = connect()
        self.providers = {n: HttpProvider(n, provider_url, dial_timeout_s) for n in ("A", "B")}
        self.safety = SafetyController(worker_id)
        self.allocator = CallAllocator(self.safety, self.providers, worker_id)
        self.next_tick: dict[int, float] = {}
        self.last_beat = self.last_reap = 0.0

    def step(self) -> int:
        now = time.monotonic()
        if now - self.last_beat >= 1.0:
            beat(self.conn, self.id)
            self.last_beat = now
        work = 0
        for camp in self.conn.execute("SELECT id, mode, settings FROM campaigns ORDER BY id").fetchall():
            cfg = CampaignSettings.from_json(camp["settings"])
            if now >= self.next_tick.get(camp["id"], 0.0):
                self.next_tick[camp["id"]] = now + cfg.pacing_tick_ms / 1000
                self.pace(camp["id"], camp["mode"], cfg)
            work += len(self.allocator.dispatch(self.conn, camp["id"], DISPATCH_PER_PASS, cfg.max_attempts))
        work += process_batch(self.conn, self.id, limit=200)
        work += sum(answer_hold.run(self.conn, self.providers, self.id).values())
        if now - self.last_reap >= 1.0:
            self.last_reap = now
            r = reaper.run(self.conn, self.providers, self.id)
            for call_id, status in r["reconciled"]:
                log.info("reconciled call %s: provider says %s", call_id[:8], status)
            work += len(r["reconciled"]) + len(r["agent_lost"])
        return work

    def pace(self, campaign_id: int, mode: str, cfg: CampaignSettings) -> None:
        token = try_lead(self.conn, campaign_id, self.id, cfg.leader_lease_ms)
        if token is None:
            return   # another worker is the pacing leader for this campaign
        breaker_store.evaluate(self.conn, campaign_id)
        snap = load_snapshot(self.conn, campaign_id, cfg, self.id, token)
        proposal = progressive.propose(snap) if mode == "PROGRESSIVE" else predictive.propose(snap, cfg)
        self.safety.decide(self.conn, campaign_id, token, proposal)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=f"worker-{os.getpid()}-{uuid.uuid4().hex[:4]}")
    ap.add_argument("--seconds", type=float, default=float("inf"))
    ap.add_argument("--provider-url", default=os.environ.get("PROVIDER_URL", "http://127.0.0.1:8001"))
    ap.add_argument("--metrics-file", help="on exit, write dispatch latencies here (load test)")
    args = ap.parse_args()
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))   # finish the pass, then exit cleanly
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.id}] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    w = Worker(args.id, args.provider_url)
    log.info("started (pid %s)", os.getpid())
    end = time.monotonic() + args.seconds
    while time.monotonic() < end and not stopping:
        try:
            if w.step() == 0:
                time.sleep(0.05)
        except Exception:
            log.error("pass failed, continuing:\n%s", traceback.format_exc())
            time.sleep(0.5)
    if args.metrics_file:
        with open(args.metrics_file, "w") as f:
            json.dump({"worker": args.id, "reserve_ms": w.allocator.reserve_latencies_ms}, f)


if __name__ == "__main__":
    main()
