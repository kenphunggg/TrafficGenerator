"""Arrival synthesis helpers."""

from __future__ import annotations

import math
import random

SECONDS_PER_TRACE_MINUTE = 60.0


def scale_count(original_count: int, scale: float) -> int:
    if original_count < 0:
        raise ValueError("original_count must be >= 0")
    if scale < 0:
        raise ValueError("scale must be >= 0")
    return int(math.floor((original_count * scale) + 0.5))


def generate_arrival_offsets(
    count: int,
    *,
    rng: random.Random | None = None,
    seconds_per_minute: float = SECONDS_PER_TRACE_MINUTE,
) -> list[float]:
    if count < 0:
        raise ValueError("count must be >= 0")
    if seconds_per_minute <= 0:
        raise ValueError("seconds_per_minute must be > 0")
    generator = rng or random.Random()
    return sorted(generator.random() * seconds_per_minute for _ in range(count))
