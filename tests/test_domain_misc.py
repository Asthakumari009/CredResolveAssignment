import random
from datetime import time

from smartdialer.domain.backoff import retry_delay_s
from smartdialer.domain.calling_window import is_open


def test_backoff_grows_and_has_a_floor():
    rng = random.Random(1)
    for attempt in range(1, 8):
        ceiling = min(3600, 30 * 2 ** (attempt - 1))
        for _ in range(100):
            d = retry_delay_s(attempt, 30, 3600, rng)
            assert ceiling / 2 <= d <= ceiling


def test_backoff_is_capped():
    assert retry_delay_s(30, 30, 3600, random.Random(1)) <= 3600


def test_calling_window():
    assert is_open(time(8, 0), "08:00", "19:00")
    assert not is_open(time(19, 0), "08:00", "19:00")
    assert not is_open(time(7, 59), "08:00", "19:00")
    assert is_open(time(23, 0), "22:00", "02:00") and is_open(time(1, 0), "22:00", "02:00")
    assert is_open(time(3, 0), "00:00", "00:00")
