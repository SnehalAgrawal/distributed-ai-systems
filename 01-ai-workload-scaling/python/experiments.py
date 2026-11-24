"""E1-E8 and negative control N1, as specified in PROTOCOL.md. Each returns plain measurements; report.py
checks them against the predictions."""

import asyncio
import collections
import math
import random

from common import CONFIG, now, task
from pool import Pool, QueueRunner, make_client_buckets
from standin import StandIn

EXP = CONFIG["experiments"]
LIMITS = CONFIG["limits"]
SEEDS = CONFIG["seeds"]


def reasons(pool):
    return dict(collections.Counter(r for _, r in pool.rejections))


def summary(pool, window):
    return {"throughput_per_minute": round(pool.throughput_per_minute(window), 3),
            "successes_in_window": sum(1 for _, e in pool.successes if window[0] <= e < window[1]),
            "attempts": pool.attempts, "rejections": len(pool.rejections), "reasons": reasons(pool),
            "max_in_flight": pool.inflight.max, "avg_in_flight": round(pool.inflight.average(*window), 3)}


async def sleep_until(t0, t):
    await asyncio.sleep(max(0.0, t - (now() - t0)))


# --- E1: Little's law -------------------------------------------------------------------------------------
async def e1():
    cfg = EXP["E1"]
    out = {"fixed": [], "variable": None}
    for n in cfg["N"]:
        p = await Pool(StandIn(None), task("tag_article"), N=n).run_for(cfg["duration"])
        w = cfg["window"]
        out["fixed"].append({"N": n, **summary(p, w), "mean_call_seconds": round(p.mean_call_seconds(w), 5)})
    v = cfg["variable"]
    p = Pool(StandIn(None), task("tag_article", call_range=v["call_seconds_range"]), N=v["N"])
    p.rng_call = random.Random(SEEDS["e1_variable"])
    await p.run_for(v["duration"])
    out["variable"] = {"N": v["N"], **summary(p, v["window"]),
                       "mean_call_seconds": round(p.mean_call_seconds(v["window"]), 5)}
    return out


# --- E2: the ceiling ---------------------------------------------------------------------------------------
async def e2():
    cfg = EXP["E2"]
    out = {"retry_after": [], "immediate": {}}
    for n in cfg["N"]:
        p = await Pool(StandIn(LIMITS), task("tag_article"), N=n, policy="R").run_for(cfg["duration"])
        out["retry_after"].append({"N": n, **summary(p, cfg["window"])})
    for rule in ("if_available", "always"):
        p = Pool(StandIn(LIMITS, rule=rule), task("tag_article"), N=cfg["immediate_N"], policy="I")
        await p.run_for(cfg["duration"])
        out["immediate"][rule] = {"N": cfg["immediate_N"], **summary(p, cfg["window"])}
    return out


# --- E2b: each limit can bind -------------------------------------------------------------------------
async def e2b():
    cfg = EXP["E2b"]
    out = {}
    for name, n in cfg["N"].items():
        out[name] = {}
        for shape, which in (("INPUT", ("input_tokens",)), ("BOTH", ("requests", "input_tokens", "output_tokens"))):
            p = Pool(StandIn(LIMITS), task(name), N=n, buckets=make_client_buckets(cfg["share"], which), policy="R")
            await p.run_for(cfg["duration"])
            out[name][shape] = {"N": n, **summary(p, cfg["window"])}
    return out


# --- E3: two limits for a pool ------------------------------------------------------------------------
async def e3():
    cfg = EXP["E3"]
    out = {}
    for speed, w in (("fast", cfg["fast_call_seconds"]), ("slow", cfg["slow_call_seconds"])):
        t = task("tag_article", call_seconds=w)
        shapes = {"C": dict(N=cfg["N"]),
                  "RATE": dict(buckets=make_client_buckets(cfg["share"])),
                  "BOTH": dict(N=cfg["N"], buckets=make_client_buckets(cfg["share"]))}
        for shape, kw in shapes.items():
            if "buckets" in kw:  # fresh buckets for each run
                kw["buckets"] = make_client_buckets(cfg["share"])
            p = await Pool(StandIn(LIMITS), t, policy="R", **kw).run_for(cfg["duration"])
            out[f"{shape}_{speed}"] = {"call_seconds": w, **summary(p, cfg["window"])}
    return out


# --- E4: burst against short-window enforcement ----------------------------------------------------------
async def e4():
    cfg = EXP["E4"]
    out = {}
    for start in ("full", "empty"):
        s = StandIn(LIMITS, window_article_seconds=CONFIG["standin"]["short_window_article_seconds"])
        p = Pool(s, task("tag_article"), N=cfg["N"],
                 buckets=make_client_buckets(cfg["share"], start_full=(start == "full")), policy="R")
        await p.run_for(cfg["duration"])
        times = [round(t, 4) for t, _ in p.rejections]
        out[start] = {**summary(p, cfg["window"]), "rejection_times": times,
                      "last_rejection": max(times) if times else None}
    return out


# --- E5: the limit is shared -------------------------------------------------------------------------------
def chat_schedule(cfg):
    rng = random.Random(SEEDS["e5_chat"])
    rate = cfg["chat_per_article_minute"] / CONFIG["scale"]["article_minute_demo_seconds"]
    t, out = cfg["chat_from"], []
    while True:
        t += rng.expovariate(rate)
        if t >= cfg["chat_to"]:
            return out
        out.append(round(t, 4))


async def chat_traffic(standin, schedule, t0, cfg):
    req = task("chat_turn")
    results = []

    async def one(at):
        await sleep_until(t0, at)
        got_429 = False
        while True:
            resp = await standin.call(req, key="chat")
            t = now() - t0
            if resp.status == 200:
                results.append({"at": at, "latency": t - at, "got_429": got_429, "ok": True})
                return
            got_429 = True
            wait = resp.headers["retry-after"]
            if t + wait + req["call_seconds"] - at > cfg["chat_deadline"]:
                results.append({"at": at, "latency": math.inf, "got_429": True, "ok": False})
                return
            await asyncio.sleep(wait)

    await asyncio.gather(*(one(at) for at in schedule))
    return results


async def e5():
    from common import percentile
    cfg = EXP["E5"]
    schedule = chat_schedule(cfg)
    out = {"chats_scheduled": len(schedule)}
    variants = {
        "a": None,
        "b": dict(N=cfg["uncapped_N"], policy="R"),
        "c": dict(N=cfg["capped_N"], buckets="share", policy="R"),
        "d": dict(N=cfg["uncapped_N"], policy="R", workspace=True),
        "e": dict(N=cfg["uncapped_N"], policy="R", separate=True),
        "f": dict(N=cfg["immediate_N"], policy="I"),
    }
    for name, v in variants.items():
        s = StandIn(LIMITS, workspaces={"import": cfg["workspace_share"]} if v and v.get("workspace") else None)
        t0 = now()
        coros = []
        pool = None
        if v:
            target = StandIn(LIMITS) if v.get("separate") else s
            buckets = make_client_buckets(cfg["share"]) if v.get("buckets") == "share" else None
            pool = Pool(target, task("tag_article"), N=v["N"], buckets=buckets, policy=v["policy"])
            pool.t0 = t0
            pool.inflight.t0 = t0
            coros.append(pool.run_for(cfg["duration"]))
        chat = asyncio.ensure_future(chat_traffic(s, schedule, t0, cfg))
        await asyncio.gather(*coros)
        results = await chat
        lat = [r["latency"] for r in results]
        p50, p95 = percentile(lat, 50), percentile(lat, 95)
        out[name] = {
            "chats": len(results),
            "chats_with_429": sum(r["got_429"] for r in results),
            "share_with_429": round(sum(r["got_429"] for r in results) / len(results), 4),
            "failures": sum(not r["ok"] for r in results),
            "p50": None if p50 is None or math.isinf(p50) else round(p50, 4),
            "p95": None if p95 is None or math.isinf(p95) else round(p95, 4),
            "p95_is_failure": bool(p95 is not None and math.isinf(p95)),
            "import": summary(pool, cfg["window"]) if pool else None,
        }
    return out


# --- E6: backpressure, age and the estimate at admission -------------------------------------------------
async def e6():
    cfg = EXP["E6"]
    t_call = task("tag_article")
    q = QueueRunner(StandIn(LIMITS), t_call, N=cfg["N"], buckets=make_client_buckets(cfg["share"]))
    t0 = q.t0
    for i in range(cfg["job1"]):
        q.enqueue(f"job1-{i:03d}", "job1", t=0.0)
    q.start()
    samples = []

    async def sampler():
        k = 0
        while True:
            await sleep_until(t0, k * cfg["sample_every"])
            samples.append({"t": round(k * cfg["sample_every"], 2), "length": q.queue_length(),
                            "age": round(q.oldest_age(), 4)})
            k += 1

    sampling = asyncio.create_task(sampler())
    await sleep_until(t0, cfg["job2_at"])
    a, b = cfg["rate_window"]
    started = sum(1 for it in q.items.values() if it.started is not None and a <= it.started < b)
    mu = started / (b - a)
    ahead = q.queue_length()
    t_admit = now() - t0
    for i in range(cfg["job2"]):
        q.enqueue(f"job2-{i:03d}", "job2")
    estimate = {"t_admit": round(t_admit, 4), "mu_per_second": round(mu, 4), "waiting_ahead": ahead,
                "first_result": round(ahead / mu + t_call["call_seconds"], 4),
                "last_result": round((ahead + cfg["job2"] - 1) / mu + t_call["call_seconds"], 4)}

    async def trickle():
        k = 0
        while True:
            at = cfg["trickle_from"] + k * cfg["trickle_every"]
            if at >= cfg["duration"]:
                return
            await sleep_until(t0, at)
            q.enqueue(f"upload-{k:04d}", "trickle")
            k += 1

    trickling = asyncio.create_task(trickle())
    await sleep_until(t0, cfg["duration"])
    sampling.cancel()
    trickling.cancel()
    await q.stop()
    job2 = [it for it in q.items.values() if it.job == "job2"]
    finished = [it.finished for it in job2 if it.finished is not None]
    actual = {"first_result": round(min(finished) - t_admit, 4) if finished else None,
              "last_result": round(max(finished) - t_admit, 4) if len(finished) == len(job2) else None,
              "job2_done": len(finished)}
    return {"estimate": estimate, "actual": actual, "samples": samples, "rejections": len(q.rejections)}


# --- E7: duplicate deliveries ------------------------------------------------------------------------------
def e7_deliveries(cfg):
    rng = random.Random(SEEDS["e7_duplicates"])
    picks = rng.sample(range(150), cfg["duplicates_adjacent"] + cfg["duplicates_late"])
    adjacent, late = set(picks[:cfg["duplicates_adjacent"]]), picks[cfg["duplicates_adjacent"]:]
    order = []
    for i in range(cfg["documents"]):
        order.append(i)
        if i in adjacent:
            order.append(i)
    order.extend(sorted(late))
    return order, sorted(adjacent), sorted(late)


async def e7():
    cfg = EXP["E7"]
    order, adjacent, late = e7_deliveries(cfg)
    out = {"deliveries": len(order), "documents": cfg["documents"], "adjacent": adjacent, "late": late}
    for variant in ("X", "Y"):
        q = QueueRunner(StandIn(None), task("tag_article"), N=cfg["N"])
        state = {"counter": 0, "calls": 0, "processed": 0, "status": {}}
        all_done = asyncio.Event()

        async def handle(item, q=q, state=state, variant=variant, all_done=all_done):
            if not (variant == "Y" and state["status"].get(item.id) == "done"):
                state["calls"] += 1
                await q.call_until_done(q._request())
                state["counter"] += 1
                state["status"][item.id] = "done"
            state["processed"] += 1
            if state["processed"] == len(order):
                all_done.set()

        q.handle = handle
        for i in order:
            q.enqueue(f"doc-{i:03d}", "job")
        q.start()
        await asyncio.wait_for(all_done.wait(), 60)
        await q.stop()
        done = sum(1 for v in state["status"].values() if v == "done")
        out[variant] = {"counter": state["counter"], "counter_percent": round(100 * state["counter"] / cfg["documents"], 1),
                        "items_done": done, "items_percent": round(100 * done / cfg["documents"], 1),
                        "model_calls": state["calls"]}
    return out


# --- E8: cancel ------------------------------------------------------------------------------------------
async def e8_level(level, cfg):
    s = StandIn(None)
    q = QueueRunner(s, task("tag_article", call_range=cfg["call_seconds_range"]), N=cfg["N"])
    q.rng_call = random.Random(SEEDS["e8_call_times"])
    state = {"fed": 0, "results": 0, "cancelled": False, "at": None}

    def cancel():
        me = asyncio.current_task()
        state["cancelled"] = True
        state["at"] = {"t_abs": now(), "in_flight": q.inflight.count, "waiting": q.queue_length(),
                       "handed_not_started": sum(1 for t in q.call_tasks.values() if t is not me) - q.inflight.count,
                       "fed": state["fed"]}
        if level >= 3:
            q.stop_starting = True
            q.waiting.clear()
        if level >= 4:
            for t in list(q.call_tasks.values()):
                if t is not me:
                    t.cancel()

    def on_finish(item):
        state["results"] += 1
        if state["results"] == cfg["cancel_after_results"]:
            cancel()

    q.on_finish = on_finish

    async def feeder():
        while state["fed"] < cfg["documents"]:
            if state["cancelled"] and level >= 2:
                return
            if q.queue_length() < cfg["feed_below"]:
                for _ in range(min(cfg["feed_chunk"], cfg["documents"] - state["fed"])):
                    q.enqueue(f"doc-{state['fed']:03d}", "job")
                    state["fed"] += 1
            await asyncio.sleep(0.01)

    feeding = asyncio.create_task(feeder())
    q.start()
    deadline = now() + 60
    while now() < deadline:
        await asyncio.sleep(0.05)
        idle = not q._tasks and (not q.waiting or q.stop_starting)
        if state["cancelled"] and feeding.done() and idle:
            break
    feeding.cancel()
    await q.stop()
    t_c = state["at"]["t_abs"]
    after = [(s.t0 + t, k, n) for t, k, n in s.admitted if s.t0 + t > t_c]
    outputs = [(s.t0 + t, n, kind) for t, _, n, kind in s.output if s.t0 + t > t_c]
    completed_after = [o for o in outputs if o[2] == "completed"]
    cancelled = [c for c in s.cancellations]
    input_after = sum(n for _, _, n in after)
    output_after = sum(n for _, n, _ in outputs)
    return {"level": level, "at_cancel": {k: v for k, v in state["at"].items() if k != "t_abs"},
            "started_after": len(after), "completed_after": len(completed_after),
            "input_tokens_after": input_after, "output_tokens_after": output_after,
            "tokens_after": input_after + output_after,
            "cancellations_seen": len(cancelled), "partial_output_tokens": [c[3] for c in cancelled],
            "documents_fed": state["fed"], "results_total": state["results"]}


async def e8():
    cfg = EXP["E8"]
    return {str(level): await e8_level(level, cfg) for level in (1, 2, 3, 4)}


# --- N1: the no-limit control ----------------------------------------------------------------------------
async def n1():
    cfg = EXP["N1"]
    out = []
    for n in cfg["N"]:
        p = await Pool(StandIn(None), task("tag_article"), N=n, policy="R").run_for(cfg["duration"])
        out.append({"N": n, **summary(p, cfg["window"])})
    return out


ALL = {"E1": e1, "E2": e2, "E2b": e2b, "E3": e3, "E4": e4, "E5": e5, "E6": e6, "E7": e7, "E8": e8, "N1": n1}
