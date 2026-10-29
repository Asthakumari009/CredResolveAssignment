"""Small exact statistics, no numpy: Wilson bound and binomial tail."""
import math


def wilson_upper(successes: int, n: int, z: float = 1.96) -> float:
    """Upper end of the Wilson score interval for a proportion. With little data it is wide,
    which is exactly what we want: uncertainty pushes the answer-rate estimate up (the dangerous
    direction), so pacing is cautious until evidence accumulates."""
    if n == 0:
        return 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / denom)


def binom_tail_above(k: int, n: int, p: float) -> float:
    """P(X > k) for X ~ Binomial(n, p), exact, summed in log space for stability."""
    if k >= n:
        return 0.0
    if k < 0:
        return 1.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(k + 1, n + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(1.0, total)
