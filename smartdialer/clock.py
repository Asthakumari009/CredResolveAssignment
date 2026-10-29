"""Wall clock for the calling-window rule, injectable so tests and demos never depend on the hour
they happen to run at. Production reads the real time. Tests, demos and the load test set
SMARTDIALER_NOW (ISO 8601 with offset); it is an environment variable, not a module global, so
worker processes they spawn see the same time."""
import os
from datetime import datetime, timezone


def now() -> datetime:
    fixed = os.environ.get("SMARTDIALER_NOW")
    return datetime.fromisoformat(fixed) if fixed else datetime.now(timezone.utc)
