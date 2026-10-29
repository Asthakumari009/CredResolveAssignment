"""Simulated humans: agents finish wrap-up after `wrap_up_s` and become AVAILABLE again.

Not part of the dialer. In production this is the agent clicking "done" in their UI; it goes
through the same agent_action() path (agent row lock only, transition logged).
"""
import threading

from smartdialer.agent_actions import agent_action
from smartdialer.db import connect


def finish_wrap_ups(conn, wrap_up_s: float) -> int:
    rows = conn.execute("SELECT id FROM agents WHERE state = 'WRAP_UP' AND state_changed_at < now() - %s * interval '1 second'",
                        (wrap_up_s,)).fetchall()
    return sum(agent_action(conn, r["id"], "AVAILABLE", "wrap-up done") for r in rows)


class AgentDesk(threading.Thread):
    def __init__(self, wrap_up_s: float = 0.5):
        super().__init__(daemon=True)
        self.wrap_up_s = wrap_up_s
        self.stopped = threading.Event()

    def run(self):
        with connect() as conn:
            while not self.stopped.wait(0.1):
                finish_wrap_ups(conn, self.wrap_up_s)

    def stop(self):
        self.stopped.set()
        self.join(timeout=5)
