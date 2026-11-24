"""Unit tests for the stand-in's rules and the pool's pieces, independent of the experiments.

    python -m unittest test_pool.py
"""

import asyncio
import time
import unittest

from common import CONFIG, InFlight, ceiling_per_minute, percentile, task
from experiments import chat_schedule, e7_deliveries
from pool import Pool, TokenBucket
from standin import StandIn

LIMITS = CONFIG["limits"]


def run(coro):
    return asyncio.run(coro)


def fast(name, **kw):
    return task(name, call_seconds=0.0, **kw)


class StandInRules(unittest.TestCase):
    def test_ceilings_match_the_protocol_table(self):
        self.assertEqual(ceiling_per_minute(task("tag_article")), (100.0, "input_tokens"))
        self.assertAlmostEqual(ceiling_per_minute(task("long_answer"))[0], 53.333, places=3)
        self.assertEqual(ceiling_per_minute(task("long_answer"))[1], "output_tokens")
        self.assertEqual(ceiling_per_minute(task("classify_title")), (1000, "requests"))

    def test_bucket_capacity_is_one_window(self):
        s = StandIn(LIMITS)  # default window: 10 article s = 1 demo s
        self.assertAlmostEqual(s.org["input_tokens"].capacity, 400000 / 6, places=3)
        short = StandIn(LIMITS, window_article_seconds=1)
        self.assertAlmostEqual(short.org["input_tokens"].capacity, 400000 / 60, places=3)

    def test_input_refusal_uses_a_request_unit_and_says_why(self):
        async def go():
            s = StandIn(LIMITS)
            admitted = 0
            while (await s.call(fast("tag_article"))).status == 200:
                admitted += 1
            before = s.org["requests"].level
            r = await s.call(fast("tag_article"))
            return admitted, before, s.org["requests"].level, r
        admitted, before, after, r = run(go())
        self.assertEqual(admitted, 16)                     # 66,667 tokens hold 16 documents of 4,000
        self.assertEqual(r.status, 429)
        self.assertEqual(r.headers["x-limit-reason"], "input_tokens")
        self.assertAlmostEqual(before - after, 1, delta=0.01)  # the rejected request still used a unit
        self.assertGreater(r.headers["retry-after"], 0)

    def test_rule_always_charges_an_empty_request_bucket(self):
        async def go(rule):
            s = StandIn(LIMITS, rule=rule)
            s.org["requests"].level = 0.0
            for _ in range(5):
                await s.call(fast("classify_title"))
            return s.org["requests"].level
        self.assertGreater(run(go("if_available")), -0.01)
        self.assertLess(run(go("always")), -4.9)

    def test_output_is_charged_at_the_end_and_can_go_into_debt(self):
        async def go():
            s = StandIn(LIMITS)
            s.org["output_tokens"].level = 100.0
            r1 = await s.call(fast("long_answer"))
            r2 = await s.call(fast("long_answer"))
            return r1.status, r2.status, r2.headers.get("x-limit-reason")
        self.assertEqual(run(go()), (200, 429, "output_tokens"))

    def test_cancellation_records_partial_output(self):
        async def go():
            s = StandIn(None)
            t = asyncio.create_task(s.call(task("tag_article", call_seconds=0.4)))
            await asyncio.sleep(0.2)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            return s.cancellations[0]
        _, _, fraction, generated = run(go())
        self.assertAlmostEqual(fraction, 0.5, delta=0.1)
        self.assertTrue(200 <= generated <= 360)

    def test_workspace_cap_refuses_before_the_organisation(self):
        async def go():
            s = StandIn(LIMITS, workspaces={"import": 0.5})
            statuses = [(await s.call(fast("tag_article"), key="import")).status for _ in range(9)]
            chat = await s.call(fast("chat_turn"), key="chat")
            return statuses, s.refused[-1][3], chat.status
        statuses, scope, chat = run(go())
        self.assertEqual(statuses.count(200), 8)           # half of 16
        self.assertEqual(scope, "workspace")
        self.assertEqual(chat, 200)


class PoolPieces(unittest.TestCase):
    def test_token_bucket_starts_empty_and_paces(self):
        async def go():
            b = TokenBucket(per_second=100, burst_seconds=0.1)
            t = time.monotonic()
            for _ in range(20):
                await b.take(1)
            return time.monotonic() - t
        self.assertAlmostEqual(run(go()), 0.2, delta=0.05)

    def test_token_bucket_refuses_a_request_larger_than_it(self):
        async def go():
            await TokenBucket(per_second=10, burst_seconds=0.5).take(6)
        with self.assertRaises(ValueError):
            run(go())

    def test_concurrency_limit_holds(self):
        async def go():
            p = Pool(StandIn(None), task("tag_article", call_seconds=0.05), N=3)
            await p.run_for(0.5)
            return p.inflight.max, len(p.successes)
        mx, done = run(go())
        self.assertEqual(mx, 3)
        self.assertGreaterEqual(done, 24)

    def test_inflight_average(self):
        f = InFlight(0.0)
        f.changes = [(0.0, 0), (1.0, 2), (3.0, 1)]
        self.assertAlmostEqual(f.average(0.0, 4.0), (0 + 4 + 1) / 4)

    def test_percentile_counts_failures_as_slowest(self):
        self.assertEqual(percentile([1, 2, 3, float("inf")], 50), 2)
        self.assertEqual(percentile([1, 2, 3, float("inf")], 95), float("inf"))

    def test_e7_duplicates_are_five_adjacent_and_five_late(self):
        order, adjacent, late = e7_deliveries(CONFIG["experiments"]["E7"])
        self.assertEqual(len(order), 210)
        self.assertEqual(len(set(adjacent) | set(late)), 10)
        self.assertTrue(all(order[order.index(i) + 1] == i for i in adjacent))
        self.assertEqual(order[-5:], late)

    def test_chat_schedule_is_seeded(self):
        cfg = CONFIG["experiments"]["E5"]
        self.assertEqual(chat_schedule(cfg), chat_schedule(cfg))
        self.assertTrue(all(cfg["chat_from"] < t < cfg["chat_to"] for t in chat_schedule(cfg)))


if __name__ == "__main__":
    unittest.main()
