"""Discrete-event simulator: virtual clock, fixed seed, no database.

It calls the SAME pure decision code as the live workers: pacing.progressive / pacing.predictive,
domain.safety_rules.decide, domain.breaker.next_state, and domain.call_sm.classify for every
call state change. What it re-implements is only the orchestration (dispatch, provider, agents),
kept as small as possible because it could drift from worker/main.py (ADR-6).

Simplifications (all in docs/SIMULATION_RESULTS.md): one pacing tick per virtual second;
borrowers are unlimited; ring, talk and latency are drawn from simple distributions; a provider
timeout means "placed, but we learn it 5 s later"; Provider B loses 2% of ANSWERED events (we
cannot bridge an answer we never hear about, so those are real abandons).
"""
import heapq
import itertools
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from smartdialer.config import CampaignSettings
from smartdialer.domain import breaker, call_sm, safety_rules
from smartdialer.domain.agent_sm import can_transition
from smartdialer.domain.types import Snapshot
from smartdialer.pacing import predictive, progressive
from sim.scenarios import Scenario

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
TERMINAL = call_sm.TERMINAL


@dataclass(frozen=True)
class ProviderModel:
    name: str
    latency_s: tuple[float, float]
    error_rate: float
    timeout_rate: float
    answered_loss: float
    outage: tuple[float, float] | None = None


PROVIDERS = {
    "A": ProviderModel("A", (0.05, 0.15), 0.01, 0.0, 0.0),
    "B": ProviderModel("B", (0.3, 2.0), 0.0, 0.08, 0.02, outage=(1800, 1980)),
}


@dataclass
class Call:
    id: int
    mode: str
    dial_t: float
    agent: int | None = None
    state: str = "QUEUED"
    answered: bool | None = None      # None until the outcome is known
    connected: bool = False
    answered_t: float | None = None
    placed: bool = True


@dataclass
class Bucket:
    dials: int = 0
    answered: int = 0
    connected: int = 0
    abandoned: int = 0
    talk_s: float = 0.0
    ticks: int = 0
    approved: int = 0
    ratio_sum: float = 0.0
    predictive_ticks: int = 0
    outcomes: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)


class Sim:
    def __init__(self, sc: Scenario, mode: str, provider: str, seed: int = 7, cfg: CampaignSettings | None = None):
        self.sc, self.mode, self.pm = sc, mode, PROVIDERS[provider]
        self.cfg = cfg or CampaignSettings(pacing_tick_ms=1000)
        self.rng = random.Random(seed)
        self.t = 0.0
        self.heap: list = []
        self.seq = itertools.count()
        self.agents = [{"state": "AVAILABLE", "call": None} for _ in range(sc.agents)]
        self.calls: list[Call] = []
        self.live: dict[int, Call] = {}
        self.cut = 0                                  # index of the oldest call with unknown outcome
        self.hold: deque[Call] = deque()              # answered, waiting for an agent
        self.abandon_win: deque[bool] = deque(maxlen=self.cfg.abandon_window)
        self.memory: dict = {}
        self.breaker_state, self.breaker_since, self.breaker_changed = "CLOSED", 0.0, False
        self.attempts: deque[tuple[float, bool]] = deque(maxlen=2000)
        self.buckets = [Bucket() for _ in range(int(sc.duration_s // 60) + 1)]
        self.decisions: list[dict] = []

    # ------------------------------------------------------------------ plumbing
    def at(self, dt, fn, *args):
        heapq.heappush(self.heap, (self.t + dt, next(self.seq), fn, args))

    def b(self, t=None) -> Bucket:
        return self.buckets[min(int((self.t if t is None else t) // 60), len(self.buckets) - 1)]

    def move(self, call: Call, target: str) -> bool:
        if call_sm.classify(call.state, target) != call_sm.Verdict.APPLY:
            return False
        call.state = target
        if target in TERMINAL:
            self.live.pop(call.id, None)
        return True

    def agent_to(self, i: int, to: str, call: Call | None = None):
        a = self.agents[i]
        assert can_transition(a["state"], to), (a["state"], to)
        a["state"], a["call"] = to, call

    def resolve(self, call: Call, answered: bool):
        if call.answered is None:
            call.answered = answered
        while self.cut < len(self.calls) and self.calls[self.cut].answered is not None:
            self.cut += 1

    def run(self):
        self.at(0, self.tick)
        while self.heap:
            t, _, fn, args = heapq.heappop(self.heap)
            if t > self.sc.duration_s:
                break
            self.t = t
            fn(*args)
        return self

    # ------------------------------------------------------------------ pacing + safety (shared pure code)
    def snapshot(self) -> Snapshot:
        live = list(self.live.values())
        window, short, i = [], 0, self.cut - 1
        while i >= 0 and len(window) < self.cfg.answer_window:
            c = self.calls[i]
            if c.answered is not None and c.placed:
                window.append(c.answered)
            i -= 1
        short_w = window[: self.cfg.short_window]
        taken = EPOCH + timedelta(seconds=self.t)
        return Snapshot(
            campaign_id=1, taken_at=taken, local_time=time(12, 0), leader_ok=True, fencing_token=1,
            breaker_state=self.breaker_state,
            available=sum(a["state"] == "AVAILABLE" for a in self.agents), staffed=len(self.agents),
            wrap_up=sum(a["state"] == "WRAP_UP" for a in self.agents),
            bound_agents=sum(a["state"] in ("RESERVED", "DIALING", "CONNECTED") for a in self.agents),
            pre_dial=sum(c.state in ("QUEUED", "RESERVED") for c in live),
            ringing=sum(c.state in ("INITIATED", "RINGING") for c in live),
            unbound_in_flight=sum(c.agent is None and c.state in ("QUEUED", "INITIATED", "RINGING") for c in live),
            answered_waiting=sum(c.state == "ANSWERED" for c in self.hold),
            connected=sum(c.state == "CONNECTED" for c in live),
            window_n=len(window), window_answered=sum(window), short_n=len(short_w), short_answered=sum(short_w),
            abandon_n=len(self.abandon_win), abandon_count=sum(self.abandon_win),
            avg_setup_s=None, avg_talk_s=None, max_staffed_recent=len(self.agents),
            breaker_age_s=self.t - self.breaker_since if self.breaker_changed else None)

    def tick(self):
        self.at(1.0, self.tick)
        self.update_breaker()
        snap = self.snapshot()
        proposal = progressive.propose(snap) if self.mode == "PROGRESSIVE" else predictive.propose(snap, self.cfg)
        decision, self.memory = safety_rules.decide(proposal, snap, self.cfg, self.memory)
        bk = self.b()
        bk.ticks += 1
        bk.approved += decision.approved_calls
        bk.outcomes[decision.outcome] = bk.outcomes.get(decision.outcome, 0) + 1
        for r in decision.reason_codes:
            bk.reasons[r] = bk.reasons.get(r, 0) + 1
        if decision.mode == "PREDICTIVE":
            bk.predictive_ticks += 1
        waiting_agents = snap.available + sum(a["state"] in ("RESERVED", "DIALING") for a in self.agents)
        if waiting_agents:   # pacing ratio: calls waiting for an answer per agent waiting for a call
            bk.ratio_sum += (snap.pre_dial + snap.ringing) / waiting_agents
        if self.sc.shock_at_s is not None and abs(self.t - self.sc.shock_at_s) <= 180:
            self.decisions.append({"t": self.t, "requested": proposal.requested_calls, "approved": decision.approved_calls,
                                   "outcome": decision.outcome, "mode": decision.mode, "reasons": ",".join(decision.reason_codes),
                                   "available": snap.available, "in_flight": snap.unbound_in_flight,
                                   "p_short": snap.short_answered / snap.short_n if snap.short_n else None,
                                   "p_long": snap.window_answered / snap.window_n if snap.window_n else None})
        self.dispatch(decision.approved_calls, decision.mode)

    def update_breaker(self):
        since = max(self.breaker_since, self.t - 120)
        recent = [ok for t, ok in self.attempts if t >= since][-20:]
        probes = [ok for t, ok in self.attempts if t >= self.breaker_since] if self.breaker_state == "HALF_OPEN" else []
        new = breaker.next_state(self.breaker_state, self.breaker_since, self.t, sum(recent), len(recent) - sum(recent),
                                 sum(probes), len(probes) - sum(probes))
        if new != self.breaker_state:
            self.breaker_state, self.breaker_since, self.breaker_changed = new, self.t, True

    # ------------------------------------------------------------------ allocator + provider model
    def dispatch(self, n: int, mode: str):
        for _ in range(n):
            agent = None
            if mode == "PROGRESSIVE":
                agent = next((i for i, a in enumerate(self.agents) if a["state"] == "AVAILABLE"), None)
                if agent is None:
                    return
            call = Call(len(self.calls), mode, self.t, agent, "RESERVED" if agent is not None else "QUEUED")
            if agent is not None:
                self.agent_to(agent, "RESERVED", call)
            self.calls.append(call)
            self.live[call.id] = call
            self.b().dials += 1
            self.at(self.rng.uniform(*self.pm.latency_s), self.dial_response, call)

    def outage(self) -> bool:
        return self.pm.outage is not None and self.pm.outage[0] <= self.t < self.pm.outage[1]

    def dial_response(self, call: Call):
        if self.outage():                      # request hung, call never placed: learn via reconcile
            call.placed = False
            self.attempts.append((self.t + 3.5, False))
            self.at(5.0, self.reconcile_unplaced, call)
            return
        if self.rng.random() < self.pm.error_rate:
            self.attempts.append((self.t, False))
            call.placed = False
            self.end(call, "FAILED", answered=False)
            return
        timed_out = self.rng.random() < self.pm.timeout_rate
        self.attempts.append((self.t + (3.5 if timed_out else 0), not timed_out))
        if not timed_out:
            self.move(call, "INITIATED")
            if call.agent is not None:
                self.agent_to(call.agent, "DIALING", call)
        p = self.sc.answer_rate(self.t)
        r = self.rng.random()
        if r < p:
            self.at(self.rng.uniform(4, 15), self.answer, call)
        elif r < p + 0.05:
            self.at(3.0, self.no_answer, call)
        else:
            self.at(30.0, self.no_answer, call)

    def reconcile_unplaced(self, call: Call):
        if self.outage():
            self.at(5.0, self.reconcile_unplaced, call)   # provider still unreachable: keep asking
            return
        self.end(call, "CANCELLED", answered=None)

    def no_answer(self, call: Call):
        self.end(call, "FAILED", answered=False)

    def answer(self, call: Call):
        call.answered_t = self.t
        self.resolve(call, True)
        self.b().answered += 1
        if self.rng.random() < self.pm.answered_loss:
            self.at(8.0, self.unseen_hangup, call)           # we never hear ANSWERED: nobody is bridged
            return
        self.move(call, "ANSWERED")
        if call.agent is not None and self.agents[call.agent]["call"] is call:
            self.agent_to(call.agent, "CONNECTED", call)
            self.connect(call)
        elif not self.claim(call):
            self.hold.append(call)
            self.at(self.cfg.max_answer_hold_ms / 1000, self.hold_timeout, call)

    def claim(self, call: Call) -> bool:
        i = next((i for i, a in enumerate(self.agents) if a["state"] == "AVAILABLE"), None)
        if i is None:
            return False
        call.agent = i
        self.agent_to(i, "CONNECTED", call)
        self.connect(call)
        return True

    def connect(self, call: Call):
        self.move(call, "CONNECTED")
        call.connected = True
        self.abandon_win.append(False)
        self.b().connected += 1
        talk = max(5.0, self.rng.expovariate(1 / self.sc.talk_mean_s(self.t)))
        start = self.t
        while start < self.t + talk:                     # attribute talk seconds to minute buckets
            end = min(self.t + talk, (math.floor(start / 60) + 1) * 60)
            self.b(start).talk_s += end - start
            start = end
        self.at(talk, self.talk_end, call)

    def talk_end(self, call: Call):
        self.move(call, "COMPLETED")
        self.agent_to(call.agent, "WRAP_UP", call)
        self.at(self.sc.wrap_up_s, self.agent_free, call.agent)

    def agent_free(self, i: int):
        self.agent_to(i, "AVAILABLE")
        while self.hold:
            c = self.hold.popleft()
            if c.state == "ANSWERED":
                self.claim(c)
                return

    def hold_timeout(self, call: Call):
        if call.state == "ANSWERED":
            self.move(call, "ABANDONED")
            self.abandon_win.append(True)
            self.b().abandoned += 1

    def unseen_hangup(self, call: Call):
        self.move(call, "COMPLETED")
        self.abandon_win.append(True)
        self.b().abandoned += 1
        if call.agent is not None and self.agents[call.agent]["call"] is call:
            self.agent_to(call.agent, "AVAILABLE")

    def end(self, call: Call, state: str, answered: bool | None):
        self.move(call, state)
        self.resolve(call, bool(answered))   # unplaced calls are excluded from the window via `placed`
        if call.agent is not None and self.agents[call.agent]["call"] is call and self.agents[call.agent]["state"] in ("RESERVED", "DIALING"):
            self.agent_to(call.agent, "AVAILABLE")

    # ------------------------------------------------------------------ results
    def summary(self) -> dict:
        warm = self.buckets[5:] or self.buckets         # skip the first 5 min warm-up
        mins = len(warm)
        answered = sum(b.answered for b in warm)
        ticks = sum(b.ticks for b in warm) or 1
        return {
            "utilization": sum(b.talk_s for b in warm) / (self.sc.agents * 60 * mins),
            "connects_per_agent_hour": sum(b.connected for b in warm) / self.sc.agents / (mins / 60),
            "dials": sum(b.dials for b in warm),
            "answered": answered,
            "abandoned": sum(b.abandoned for b in warm),
            "abandon_rate": sum(b.abandoned for b in warm) / answered if answered else 0.0,
            "predictive_share": sum(b.predictive_ticks for b in warm) / ticks,
        }
