"""Checks added after the reported runs, during the review (deviation D7). Not part of the frozen protocol.

    python after_freeze.py > ../results/after-freeze.txt

They re-run frozen experiment code with longer windows or extra counts, to test three explanations in the README and
the article. Their numbers are labelled "after the freeze" wherever they are used.

1. E2b long answers with an input bucket only: is 36/min a window artifact? Run 40 s and count over longer windows.
2. E5 b and f: why did chat do worse in b? Count chat's refusals by reason.
3. E6: how long did a document arriving after job 2 wait before it started?
"""

import asyncio
import collections
import math

import experiments as X
import pool as P
from common import CONFIG, now, task
from standin import StandIn


async def e2b_long():
    p = P.Pool(StandIn(CONFIG["limits"]), task("long_answer"), N=36,
               buckets=P.make_client_buckets(0.95, ("input_tokens",)), policy="R")
    await p.run_for(40.0)
    starts = sorted(a for a, _ in p.successes)
    ends = [b for _, b in p.successes]
    print("1. E2b long_answer, input bucket only, 40 s run")
    print(f"   first 36 starts span {starts[0]:.2f} to {starts[35]:.2f} demo s")
    for w in ((6, 18), (6, 30), (6, 40), (10, 40)):
        n = sum(1 for e in ends if w[0] <= e < w[1])
        print(f"   window {w}: {n} completions = {n / (w[1] - w[0]) * 6:.1f} per article minute")


async def e5_reasons():
    cfg = X.EXP["E5"]
    print("2. E5 chat refusals by reason")
    for label, n, policy in (("b (C(33), retry-after)", 33, "R"), ("f (C(67), immediate)", 67, "I")):
        s = StandIn(X.LIMITS)
        t0 = now()
        p = P.Pool(s, task("tag_article"), N=n, policy=policy)
        p.t0 = t0
        p.inflight.t0 = t0
        chat = asyncio.ensure_future(X.chat_traffic(s, X.chat_schedule(cfg), t0, cfg))
        await p.run_for(cfg["duration"])
        res = await chat
        reasons = dict(collections.Counter(r for _, k, r, _ in s.refused if k == "chat"))
        lat = sorted(r["latency"] for r in res)
        p95 = lat[math.ceil(0.95 * len(lat)) - 1]
        print(f"   {label}: chat refusals {reasons}; chats with a 429 {sum(r['got_429'] for r in res)}/{len(res)}; "
              f"p95 {p95:.3f} demo s; failures {sum(not r['ok'] for r in res)}")


async def e6_waits():
    captured = {}
    orig = P.QueueRunner.stop

    async def stop(self):
        captured["q"] = self
        await orig(self)

    P.QueueRunner.stop = stop
    try:
        r = await X.e6()
    finally:
        P.QueueRunner.stop = orig
    q = captured["q"]
    print("3. E6 wait before start of single uploads, by arrival time")
    for lo in (6, 8, 10, 11):
        waits = [it.started - it.enqueued for it in q.items.values()
                 if it.job == "trickle" and lo <= it.enqueued < lo + 0.5 and it.started is not None]
        if waits:
            print(f"   arrived t={lo}-{lo + 0.5}: {len(waits)} started, wait {min(waits):.2f}-{max(waits):.2f} demo s")
    for t in (8, 16, 24, 34):
        s = next(x for x in r["samples"] if x["t"] == t)
        print(f"   t={t}: length {s['length']}, oldest age {s['age']:.2f} demo s")


async def main():
    await e2b_long()
    await e5_reasons()
    await e6_waits()


if __name__ == "__main__":
    asyncio.run(main())
