"""Shared helpers: the configuration, unit conversions, and small statistics."""

import json
import math
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE.parent
CONFIG = json.loads((DEMO / "data" / "config.json").read_text())
MINUTE = CONFIG["scale"]["article_minute_demo_seconds"]  # demo seconds in one article minute (6.0)


def per_second(per_article_minute):
    """A limit per article minute as a rate per demo second."""
    return per_article_minute / MINUTE


def per_article_minute(per_demo_second):
    return per_demo_second * MINUTE


def task(name, **override):
    """A task's sizes and call time, with optional overrides (call_seconds=..., call_range=...)."""
    t = dict(CONFIG["tasks"][name], name=name)
    t.update(override)
    return t


def ceiling_per_minute(t, limits=None, share=1.0):
    """Documents per article minute the limits allow for task t, and which limit binds."""
    limits = limits or CONFIG["limits"]
    allowed = {"requests": limits["requests"] * share,
               "input_tokens": limits["input_tokens"] * share / t["input_tokens"],
               "output_tokens": limits["output_tokens"] * share / t["output_tokens"]}
    binding = min(allowed, key=allowed.get)
    return allowed[binding], binding


def now():
    return time.monotonic()


def percentile(values, q):
    """Nearest-rank percentile; inf values sort last (a failed chat counts as slower than any success)."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, math.ceil(q / 100 * len(s)) - 1)
    return s[k]


class InFlight:
    """Counts calls in flight and keeps every change, so the time average over any window can be computed."""

    def __init__(self, t0):
        self.t0 = t0
        self.count = 0
        self.max = 0
        self.changes = [(0.0, 0)]

    def change(self, delta):
        self.count += delta
        self.max = max(self.max, self.count)
        self.changes.append((now() - self.t0, self.count))

    def average(self, start, end):
        area, last_t, last_c = 0.0, None, 0
        for t, c in self.changes:
            if last_t is not None:
                a, b = max(last_t, start), min(t, end)
                if b > a:
                    area += (b - a) * last_c
            last_t, last_c = t, c
        if last_t is not None and end > max(last_t, start):
            area += (end - max(last_t, start)) * last_c
        return area / (end - start)
