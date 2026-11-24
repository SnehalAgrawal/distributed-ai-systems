"""The worker pool under test: a concurrency limit, client-side token buckets, and a 429 policy.

`TokenBucket` is the article's snippet. `make_client_buckets` gives one bucket per provider limit.
`Pool.run_for` keeps a pool busy for a fixed time on an endless supply of one task (E1-E5);
`Pool.run_queue` works through a FIFO queue of items that can grow while it runs (E6-E8).
"""

import asyncio
import collections
import time

from common import CONFIG, InFlight, now, per_second


class TokenBucket:
    """At most `per_second` units per second, with bursts of at most `burst_seconds` worth.
    Starts empty unless start_full, so a new job ramps up instead of firing a burst."""

    def __init__(self, per_second: float, burst_seconds: float = 0.5, start_full: bool = False):
        self.rate = per_second
        self.capacity = self.rate * burst_seconds
        self.available = self.capacity if start_full else 0.0
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, n: float) -> None:
        if n > self.capacity:
            raise ValueError(f"a request of {n} exceeds the bucket ({self.capacity:.0f})")
        async with self.lock:                         # waiters are served in arrival order
            while True:
                t = time.monotonic()
                self.available = min(self.capacity, self.available + (t - self.updated) * self.rate)
                self.updated = t
                if self.available >= n:
                    self.available -= n
                    return
                await asyncio.sleep((n - self.available) / self.rate)


def make_client_buckets(share, which=("requests", "input_tokens", "output_tokens"), start_full=False,
                        limits=None):
    limits = limits or CONFIG["limits"]
    burst = CONFIG["pool"]["bucket_burst_seconds"]
    return {k: TokenBucket(per_second(limits[k] * share), burst, start_full) for k in which}


def cost(bucket_name, t):
    return 1 if bucket_name == "requests" else t[bucket_name]


class Pool:
    """N: concurrency limit (None = no limit). buckets: dict from make_client_buckets (None = no rate limit).
    policy: "R" waits retry-after on a 429, "I" retries after a fixed short pause."""

    def __init__(self, standin, task, N=None, buckets=None, policy="R", key="import"):
        self.standin = standin
        self.task = task
        self.sem = asyncio.Semaphore(N) if N else None
        self.buckets = buckets or {}
        self.policy = policy
        self.key = key
        self.t0 = now()
        self.inflight = InFlight(self.t0)
        self.successes = []   # (t_start, t_end) of each successful call, demo seconds since t0
        self.attempts = 0
        self.rejections = []  # (t, reason)
        self._tasks = set()
        self.rng_call = None  # random.Random for call times drawn from task["call_range"]

    def _request(self):
        req = dict(self.task)
        if "call_range" in req and self.rng_call is not None:
            lo, hi = req["call_range"]
            req["call_seconds"] = self.rng_call.uniform(lo, hi)
        return req

    async def _take(self):
        for name, b in self.buckets.items():
            await b.take(cost(name, self.task))

    async def _attempt(self, req):
        """One request to the stand-in; returns the response. Counts in-flight only while the call runs."""
        self.attempts += 1
        t_start = now() - self.t0
        self.inflight.change(+1)
        try:
            resp = await self.standin.call(req, self.key)
        finally:
            self.inflight.change(-1)
        if resp.status == 200:
            self.successes.append((t_start, now() - self.t0))
        else:
            self.rejections.append((now() - self.t0, resp.headers["x-limit-reason"]))
        return resp

    async def call_until_done(self, req, first_taken=True):
        """Call until success; every attempt after the first takes from the buckets again."""
        if not first_taken:
            await self._take()
        while True:
            resp = await self._attempt(req)
            if resp.status == 200:
                return resp
            if self.policy == "R":
                await asyncio.sleep(resp.headers["retry-after"])
            else:
                await asyncio.sleep(CONFIG["pool"]["immediate_retry_pause_seconds"])
            await self._take()

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _one(self, req):
        try:
            await self.call_until_done(req)
        finally:
            if self.sem:
                self.sem.release()

    async def _dispatch_forever(self):
        while True:
            if self.sem:
                await self.sem.acquire()
            await self._take()
            self._spawn(self._one(self._request()))

    async def run_for(self, duration):
        """Keep the pool busy for `duration` demo seconds, then cancel everything still running."""
        dispatcher = asyncio.create_task(self._dispatch_forever())
        await asyncio.sleep(duration - (now() - self.t0))
        dispatcher.cancel()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(dispatcher, *self._tasks, return_exceptions=True)
        return self

    # --- summaries ------------------------------------------------------------------------------------
    def throughput_per_minute(self, window):
        a, b = window
        done = sum(1 for _, end in self.successes if a <= end < b)
        return done / (b - a) * CONFIG["scale"]["article_minute_demo_seconds"]

    def mean_call_seconds(self, window):
        a, b = window
        d = [end - start for start, end in self.successes if a <= end < b]
        return sum(d) / len(d) if d else None


class Item:
    __slots__ = ("id", "job", "enqueued", "status", "started", "finished")

    def __init__(self, item_id, job, enqueued):
        self.id, self.job, self.enqueued = item_id, job, enqueued
        self.status, self.started, self.finished = "waiting", None, None


class QueueRunner(Pool):
    """A FIFO queue of items worked by the pool. `handler(item)` runs one item (default: call the model)."""

    def __init__(self, standin, task, N=None, buckets=None, policy="R", key="import"):
        super().__init__(standin, task, N, buckets, policy, key)
        self.waiting = collections.deque()
        self.items = {}
        self.arrived = asyncio.Event()
        self.stop_starting = False       # set by a cancel that skips queued work
        self.call_tasks = {}             # item id -> task, for cancelling calls in flight
        self.on_finish = None            # callback(item)

    def enqueue(self, item_id, job, t=None):
        item = Item(item_id, job, (now() - self.t0) if t is None else t)
        self.items.setdefault(item_id, item)
        self.waiting.append(item)
        self.arrived.set()
        return item

    def queue_length(self):
        return len(self.waiting)

    def oldest_age(self):
        return (now() - self.t0 - self.waiting[0].enqueued) if self.waiting else 0.0

    async def handle(self, item):
        await self.call_until_done(self._request())

    async def _run_item(self, item):
        try:
            if self.stop_starting:       # handed to a worker, but the job was cancelled before its call began
                item.status = "skipped"
                return
            item.status = "running"
            item.started = item.started if item.started is not None else now() - self.t0
            await self.handle(item)
            item.status = "done"
            item.finished = now() - self.t0
            if self.on_finish:
                self.on_finish(item)
        except asyncio.CancelledError:
            item.status = "cancelled"
            raise
        finally:
            self.call_tasks.pop(id(item), None)
            if self.sem:
                self.sem.release()

    async def _dispatch_queue(self):
        while True:
            while not self.waiting:
                self.arrived.clear()
                await self.arrived.wait()
            if self.sem:
                await self.sem.acquire()
            if self.stop_starting:
                if self.sem:
                    self.sem.release()
                return
            await self._take()
            if self.stop_starting or not self.waiting:
                if self.sem:
                    self.sem.release()
                if self.stop_starting:
                    return
                continue
            item = self.waiting.popleft()
            self.call_tasks[id(item)] = self._spawn(self._run_item(item))

    def start(self):
        self.dispatcher = asyncio.create_task(self._dispatch_queue())

    async def stop(self):
        self.dispatcher.cancel()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(self.dispatcher, *self._tasks, return_exceptions=True)
