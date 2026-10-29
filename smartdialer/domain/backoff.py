import random


def retry_delay_s(attempt: int, base_s: float, cap_s: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with "equal jitter": half the delay is fixed, half is random.

    Plain full jitter can return ~0 and redial a borrower immediately; equal jitter keeps a
    floor while still spreading retries so a provider outage does not end in a thundering herd.
    attempt is 1 for the first retry.
    """
    rng = rng or random
    ceiling = min(cap_s, base_s * 2 ** max(0, attempt - 1))
    return ceiling / 2 + rng.uniform(0, ceiling / 2)
