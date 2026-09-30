from __future__ import annotations

import math
from typing import Any


def network_sample_is_stale(network: dict[str, Any]) -> bool:
    """Return whether cached network counters are too old for causal comparison."""
    age = network.get("sample_age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
        return False

    interval = network.get("sample_interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval):
        max_age = 30.0
    else:
        max_age = max(float(interval) * 2, 15.0)
    return age > max_age
