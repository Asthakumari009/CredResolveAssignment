"""Named crash points for the failure demos and tests.

When SMARTDIALER_CRASH_AT=<point> is set, the process SIGKILLs itself on reaching that point:
no finally blocks, no atexit, no clean disconnect. Postgres sees the socket drop and rolls back
any open transaction, exactly as with a real crash. Unset (production), this is a no-op.
"""
import os
import signal


def crash_point(name: str) -> None:
    if os.environ.get("SMARTDIALER_CRASH_AT") == name:
        print(f"[fault] {os.getpid()} crashing at {name}", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
