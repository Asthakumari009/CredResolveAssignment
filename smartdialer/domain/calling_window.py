from datetime import time


def is_open(local: time, start: str, end: str) -> bool:
    """True if `local` (campaign timezone) is inside [start, end). Windows may wrap midnight."""
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    if s == e:
        return True   # "00:00"-"00:00" means always open (used by demos and the simulator)
    if s < e:
        return s <= local < e
    return local >= s or local < e
