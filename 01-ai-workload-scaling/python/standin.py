"""A stand-in for a hosted model's rate limiter. Not OpenAI's or Anthropic's limiter: a configured one.

It follows what their documentation describes, as configured in PROTOCOL.md ("The stand-in's rules"):
separate request, input-token and output-token limits per organisation; token buckets that refill
continuously up to a capacity of `window` article seconds' worth; a rejected request that still uses a
request unit; 429s with retry-after and remaining-capacity headers. It is a Python object called with
`await` in the same process, not a server: a client that goes away is a cancelled call.
"""

import asyncio
import math
from dataclasses import dataclass, field

from common import CONFIG, now, per_second

LIMITS = ("requests", "input_tokens", "output_tokens")


class Bucket:
    """Refills continuously at `rate` per demo second, up to `capacity`. The level may go below zero (debt)."""

    def __init__(self, rate, capacity, start_full=True):
        self.rate = rate
        self.capacity = capacity
        self.level = capacity if start_full else 0.0
        self.updated = now()

    def refill(self, t):
        self.level = min(self.capacity, self.level + (t - self.updated) * self.rate)
        self.updated = t


def make_buckets(limits, window_article_seconds, share=1.0):
    window = window_article_seconds / CONFIG["scale"]["article_seconds_per_demo_second"]  # demo seconds
    return {k: Bucket(per_second(limits[k] * share), per_second(limits[k] * share) * window) for k in LIMITS}


@dataclass
class Response:
    status: int
    headers: dict
    usage: dict = field(default_factory=dict)


class StandIn:
    """`limits=None` admits everything (no-limit mode). `workspaces` maps an API key to a share of each
    organisation limit, enforced before the organisation's buckets (rule 6)."""

    def __init__(self, limits=None, window_article_seconds=None, rule=None, workspaces=None):
        self.unlimited = limits is None
        window = window_article_seconds or CONFIG["standin"]["window_article_seconds"]
        self.rule = rule or CONFIG["standin"]["rejected_rule"]
        self.org = None if self.unlimited else make_buckets(limits, window)
        self.workspaces = {key: make_buckets(limits, window, share) for key, share in (workspaces or {}).items()}
        self.t0 = now()
        # Logs, in demo seconds since t0
        self.admitted = []      # (t, key, input_tokens)
        self.output = []        # (t, key, output_tokens, "completed" | "cancelled")
        self.refused = []       # (t, key, reason, scope)
        self.cancellations = []  # (t, key, fraction_of_call, output_tokens_generated)

    # --- admission (synchronous: nothing else runs in between) ---------------------------------------
    @staticmethod
    def _check(b, req):
        if b["requests"].level < 1:
            return "requests", (1 - b["requests"].level) / b["requests"].rate
        if b["input_tokens"].level < req["input_tokens"]:
            return "input_tokens", (req["input_tokens"] - b["input_tokens"].level) / b["input_tokens"].rate
        if b["output_tokens"].level <= 0:
            return "output_tokens", (1 - b["output_tokens"].level) / b["output_tokens"].rate
        return None

    def _charge_rejection(self, b, reason):
        if self.rule == "always" or reason != "requests":
            b["requests"].level -= 1

    @staticmethod
    def _remaining(b):
        return {f"x-remaining-{k.replace('_', '-')}": math.floor(b[k].level) for k in LIMITS}

    def _admit(self, req, key, t):
        scopes = []
        if key in self.workspaces:
            scopes.append(("workspace", self.workspaces[key]))
        scopes.append(("organisation", self.org))
        for _, b in scopes:
            for bucket in b.values():
                bucket.refill(t)
        for scope, b in scopes:
            refusal = self._check(b, req)
            if refusal:
                reason, wait = refusal
                self._charge_rejection(b, reason)
                self.refused.append((t - self.t0, key, reason, scope))
                headers = {"retry-after": round(wait + 0.0005, 3), "x-limit-reason": reason,
                           "x-limit-scope": scope, **self._remaining(b)}
                return Response(429, headers)
        for _, b in scopes:
            b["requests"].level -= 1
            b["input_tokens"].level -= req["input_tokens"]
        return None

    def _charge_output(self, key, tokens, t):
        if self.unlimited:
            return
        scopes = [self.org] + ([self.workspaces[key]] if key in self.workspaces else [])
        for b in scopes:
            b["output_tokens"].refill(t)
            b["output_tokens"].level -= tokens

    # --- the call ----------------------------------------------------------------------------------------
    async def call(self, req, key="import"):
        """req: input_tokens, output_tokens, call_seconds. Returns a Response; raises CancelledError if the
        caller cancels, after recording what had been generated."""
        t = now()
        if not self.unlimited:
            refusal = self._admit(req, key, t)
            if refusal:
                return refusal
        self.admitted.append((t - self.t0, key, req["input_tokens"]))
        try:
            await asyncio.sleep(req["call_seconds"])
        except asyncio.CancelledError:
            t_end = now()
            fraction = min(1.0, (t_end - t) / req["call_seconds"]) if req["call_seconds"] else 1.0
            generated = math.floor(fraction * req["output_tokens"])
            self._charge_output(key, generated, t_end)
            self.cancellations.append((t_end - self.t0, key, round(fraction, 4), generated))
            self.output.append((t_end - self.t0, key, generated, "cancelled"))
            raise
        t_end = now()
        self._charge_output(key, req["output_tokens"], t_end)
        self.output.append((t_end - self.t0, key, req["output_tokens"], "completed"))
        headers = self._remaining(self.org) if not self.unlimited else {}
        return Response(200, headers, {"input_tokens": req["input_tokens"], "output_tokens": req["output_tokens"]})
