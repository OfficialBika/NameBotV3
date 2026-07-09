from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class Counter:
    total: int = 0
    hits: int = 0
    misses: int = 0
    errors: int = 0
    ema_ms: float = 0.0

    def record(self, ms: float, *, hit: bool = False, error: bool = False) -> None:
        self.total += 1
        if error:
            self.errors += 1
        elif hit:
            self.hits += 1
        else:
            self.misses += 1
        self.ema_ms = ms if self.ema_ms == 0 else 0.15 * ms + 0.85 * self.ema_ms


class PerfTracker:
    def __init__(self) -> None:
        self.lookup = Counter()
        self.started_at = time.time()

    def snapshot(self) -> Dict[str, float | int]:
        uptime = max(1, int(time.time() - self.started_at))
        return {
            "uptime_sec": uptime,
            "lookup_total": self.lookup.total,
            "lookup_hits": self.lookup.hits,
            "lookup_misses": self.lookup.misses,
            "lookup_errors": self.lookup.errors,
            "lookup_ema_ms": round(self.lookup.ema_ms, 2),
        }


perf = PerfTracker()
