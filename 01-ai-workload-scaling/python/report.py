"""Check one run against PROTOCOL.md's predictions: python report.py runs/<stamp> [--negative-control]

Prints and saves runs/<stamp>/report.txt with three parts: the run's validity (freeze, calibration), a stable
section (PASS/FAIL of every prediction plus counts that don't depend on timing; compared across runs by SHA-256),
and the measurements, which vary from run to run. Exits 1 if any prediction fails.

--negative-control changes two expectations on purpose (P2.2 expects no 429s at N = 67; P7 expects the counter in
variant X to read 200). Both must then FAIL and the exit status must be 1.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from common import CONFIG, ceiling_per_minute, task

TOL = CONFIG["tolerances"]
EXP = CONFIG["experiments"]


def within(x, target, rel):
    return x is not None and abs(x - target) <= rel * abs(target)


def share(reasons, key):
    total = sum(reasons.values())
    return reasons.get(key, 0) / total if total else 0.0


def fmt(x, nd=1):
    return "none" if x is None else f"{x:.{nd}f}"


def evaluate(obs, negative=False):
    E = obs["experiments"]
    stable, meas = [], []

    def check(pid, title, ok, detail=""):
        stable.append(f"{'PASS' if ok else 'FAIL'} {pid} {title}" + (f" | {detail}" if detail else ""))

    # E1 ---------------------------------------------------------------------------------------------------
    if "E1" in E:
        w = task("tag_article")["call_seconds"]
        for r in E["E1"]["fixed"]:
            pred = 6 * r["N"] / w
            check("P1.1", f"E1 N={r['N']} fixed W: throughput within 5% of N/W ({pred:g}/min)",
                  within(r["throughput_per_minute"], pred, TOL["throughput"]))
            lam = r["throughput_per_minute"] / 6
            check("P1.2", f"E1 N={r['N']} fixed W: average in flight within 5% of throughput x mean call time",
                  within(r["avg_in_flight"], lam * r["mean_call_seconds"], TOL["throughput"]))
            meas.append(f"E1 N={r['N']}: throughput/min {r['throughput_per_minute']:.2f}, avg in flight "
                        f"{r['avg_in_flight']:.3f}, mean call s {r['mean_call_seconds']:.4f}, "
                        f"throughput x call {lam * r['mean_call_seconds']:.3f}")
        v = E["E1"]["variable"]
        pred = 6 * v["N"] / 1.0
        lam = v["throughput_per_minute"] / 6
        check("P1.1", f"E1 N={v['N']} random W in [0.5, 1.5]: throughput within 8% of N/mean W ({pred:g}/min)",
              within(v["throughput_per_minute"], pred, TOL["throughput_random"]))
        check("P1.2", f"E1 N={v['N']} random W: average in flight within 8% of throughput x mean call time",
              within(v["avg_in_flight"], lam * v["mean_call_seconds"], TOL["throughput_random"]))
        meas.append(f"E1 N={v['N']} random W: throughput/min {v['throughput_per_minute']:.2f}, avg in flight "
                    f"{v['avg_in_flight']:.3f}, mean call s {v['mean_call_seconds']:.4f}, "
                    f"throughput x call {lam * v['mean_call_seconds']:.3f}")

    # E2 ---------------------------------------------------------------------------------------------------
    if "E2" in E:
        ceil, _ = ceiling_per_minute(task("tag_article"))
        by_n = {r["N"]: r for r in E["E2"]["retry_after"]}
        for r in E["E2"]["retry_after"]:
            meas.append(f"E2 C({r['N']}) retry-after: throughput/min {r['throughput_per_minute']:.2f}, 429s "
                        f"{r['rejections']} (input_tokens {r['reasons'].get('input_tokens', 0)}, requests "
                        f"{r['reasons'].get('requests', 0)}), attempts {r['attempts']}, max in flight {r['max_in_flight']}")
        for n in (8, 16):
            r = by_n[n]
            check("P2.1", f"E2 C({n}) below L*: throughput within 5% of {6 * n}/min, no 429s",
                  within(r["throughput_per_minute"], 6 * n, TOL["throughput"]) and r["rejections"] == 0)
        for n in (33, 67):
            r = by_n[n]
            check("P2.2", f"E2 C({n}) above L*: throughput within 5% of the ceiling ({ceil:g}/min)",
                  within(r["throughput_per_minute"], ceil, TOL["throughput"]))
        r33, r67 = by_n[33], by_n[67]
        if negative:
            check("P2.2", "E2 C(67): no 429s [NEGATIVE CONTROL: expectation changed on purpose]",
                  r67["rejections"] == 0)
        else:
            check("P2.2", "E2 429s at N=33 and N=67, more at 67 than at 33",
                  0 < r33["rejections"] < r67["rejections"])
        check("P2.2", "E2 at least 95% of 429s at N=33 and 67 have reason input_tokens",
              share(r33["reasons"], "input_tokens") >= 0.95 and share(r67["reasons"], "input_tokens") >= 0.95)
        for rule, r in E["E2"]["immediate"].items():
            check("P2.3", f"E2 C(67) immediate retries, rule {rule}: useful throughput more than 5% below "
                          f"the retry-after result", r["throughput_per_minute"] < 0.95 * r67["throughput_per_minute"])
            meas.append(f"E2 C(67) immediate, rule {rule}: throughput/min {r['throughput_per_minute']:.2f}, 429s "
                        f"{r['rejections']}, attempts {r['attempts']}, reasons {json.dumps(r['reasons'], sort_keys=True)}")

    # E2b --------------------------------------------------------------------------------------------------
    if "E2b" in E:
        s = EXP["E2b"]["share"]
        expected_reason = {"long_answer": "output_tokens", "classify_title": "requests"}
        for name, shapes in E["E2b"].items():
            ceil, binding = ceiling_per_minute(task(name))
            b, i = shapes["BOTH"], shapes["INPUT"]
            check("P2b.1", f"E2b {name} BOTH: throughput within 5% of {s * ceil:g}/min, no 429s",
                  within(b["throughput_per_minute"], s * ceil, TOL["throughput"]) and b["rejections"] == 0,
                  f"binding limit by configuration: {binding}")
            if name == "tag_article":
                check("P2b.2", f"E2b {name} INPUT: like BOTH ({s * ceil:g}/min, no 429s)",
                      within(i["throughput_per_minute"], s * ceil, TOL["throughput"]) and i["rejections"] == 0)
            else:
                reason = expected_reason[name]
                check("P2b.2", f"E2b {name} INPUT: 429s, >= 95% with reason {reason}, throughput within 5% of "
                               f"{ceil:g}/min", i["rejections"] > 0 and share(i["reasons"], reason) >= 0.95
                      and within(i["throughput_per_minute"], ceil, TOL["throughput"]))
            for shape, r in (("INPUT", i), ("BOTH", b)):
                meas.append(f"E2b {name} {shape}: throughput/min {r['throughput_per_minute']:.2f}, 429s "
                            f"{r['rejections']}, reasons {json.dumps(r['reasons'], sort_keys=True)}")

    # E3 ---------------------------------------------------------------------------------------------------
    if "E3" in E:
        r = E["E3"]
        n = EXP["E3"]["N"]
        slow_w = EXP["E3"]["slow_call_seconds"]
        c_slow = 6 * n / slow_w
        rate95 = 0.95 * ceiling_per_minute(task("tag_article"))[0]
        check("P3", "E3 C(20) fast: 429s, throughput within 5% of 100/min",
              r["C_fast"]["rejections"] > 0 and within(r["C_fast"]["throughput_per_minute"], 100, TOL["throughput"]))
        check("P3", f"E3 C(20) slow: no 429s, throughput within 5% of {c_slow:g}/min, at most 20 in flight",
              r["C_slow"]["rejections"] == 0 and within(r["C_slow"]["throughput_per_minute"], c_slow, TOL["throughput"])
              and r["C_slow"]["max_in_flight"] <= n)
        check("P3", f"E3 RATE fast: no 429s, throughput within 5% of {rate95:g}/min",
              r["RATE_fast"]["rejections"] == 0 and within(r["RATE_fast"]["throughput_per_minute"], rate95, TOL["throughput"]))
        check("P3", f"E3 RATE slow: no 429s, throughput within 5% of {rate95:g}/min, max in flight > 20, "
                    f"average in flight within 10% of {rate95 / 6 * slow_w:.1f}",
              r["RATE_slow"]["rejections"] == 0 and within(r["RATE_slow"]["throughput_per_minute"], rate95, TOL["throughput"])
              and r["RATE_slow"]["max_in_flight"] > n and within(r["RATE_slow"]["avg_in_flight"], rate95 / 6 * slow_w, 0.10))
        check("P3", f"E3 BOTH fast: no 429s, throughput within 5% of {rate95:g}/min, at most 20 in flight",
              r["BOTH_fast"]["rejections"] == 0 and within(r["BOTH_fast"]["throughput_per_minute"], rate95, TOL["throughput"])
              and r["BOTH_fast"]["max_in_flight"] <= n)
        check("P3", f"E3 BOTH slow: no 429s, throughput within 5% of {c_slow:g}/min, at most 20 in flight",
              r["BOTH_slow"]["rejections"] == 0 and within(r["BOTH_slow"]["throughput_per_minute"], c_slow, TOL["throughput"])
              and r["BOTH_slow"]["max_in_flight"] <= n)
        for k in ("C_fast", "C_slow", "RATE_fast", "RATE_slow", "BOTH_fast", "BOTH_slow"):
            x = r[k]
            # Deviation D1 (added after development runs, before the code freeze): with fixed call times, calls
            # finish in lockstep batches, so a window that isn't a whole number of call times over- or under-counts.
            # Little's law gives the rate from the same run without that: average in flight / call time.
            little = x["avg_in_flight"] / r[k]["call_seconds"] * 6
            meas.append(f"E3 {k}: throughput/min {x['throughput_per_minute']:.2f}, 429s {x['rejections']}, "
                        f"max in flight {x['max_in_flight']}, avg in flight {x['avg_in_flight']:.2f}, "
                        f"avg in flight / call time per min {little:.2f}")

    # E4 ---------------------------------------------------------------------------------------------------
    if "E4" in E:
        f, e = E["E4"]["full"], E["E4"]["empty"]
        rate95 = 0.95 * ceiling_per_minute(task("tag_article"))[0]
        check("P4", "E4 buckets start full: at least one 429, all within the first 1.0 s",
              f["rejections"] >= 1 and f["last_rejection"] is not None and f["last_rejection"] < EXP["E4"]["start_window"])
        check("P4", "E4 buckets start empty: no 429s", e["rejections"] == 0)
        check("P4", f"E4 both: throughput over [2, 8] within 5% of {rate95:g}/min",
              within(f["throughput_per_minute"], rate95, TOL["throughput"])
              and within(e["throughput_per_minute"], rate95, TOL["throughput"]))
        for k, x in (("full", f), ("empty", e)):
            meas.append(f"E4 start {k}: 429s {x['rejections']}, last 429 at s {fmt(x['last_rejection'], 3)}, "
                        f"throughput/min {x['throughput_per_minute']:.2f}")

    # E5 ---------------------------------------------------------------------------------------------------
    if "E5" in E:
        r = E["E5"]
        a = r["a"]
        a95 = a["p95"]

        def p95_at_least(x, factor):
            return x["p95_is_failure"] or (x["p95"] is not None and a95 is not None and x["p95"] >= factor * a95)

        def p95_at_most(x, factor):
            return (not x["p95_is_failure"]) and x["p95"] is not None and a95 is not None and x["p95"] <= factor * a95

        imp = lambda x: x["import"]["throughput_per_minute"] if x["import"] else None  # noqa: E731
        check("P5", "E5a chat alone: no chat 429s", a["chats_with_429"] == 0,
              f"chats scheduled {r['chats_scheduled']}")
        check("P5", "E5b import uncapped: >= 25% of chats get a 429, chat p95 >= 1.5 x a's",
              r["b"]["share_with_429"] >= 0.25 and p95_at_least(r["b"], 1.5))
        check("P5", "E5c import at 60%: <= 5% of chats get a 429, chat p95 <= 1.2 x a's, import within 5% of 60/min",
              r["c"]["share_with_429"] <= 0.05 and p95_at_most(r["c"], 1.2) and within(imp(r["c"]), 60, TOL["throughput"]))
        check("P5", "E5d workspace cap at 60%: <= 5% of chats get a 429, import gets 429s, import within 5% of 60/min",
              r["d"]["share_with_429"] <= 0.05 and r["d"]["import"]["rejections"] > 0
              and within(imp(r["d"]), 60, TOL["throughput"]))
        check("P5", "E5e import on a separate limit: no chat 429s, import within 5% of 100/min",
              r["e"]["chats_with_429"] == 0 and within(imp(r["e"]), 100, TOL["throughput"]))
        check("P5", "E5f import uncapped, immediate retries: share of chats with a 429 >= b's",
              r["f"]["share_with_429"] >= r["b"]["share_with_429"])
        for k in "abcdef":
            x = r[k]
            p95 = "failed" if x["p95_is_failure"] else fmt(x["p95"], 3)
            line = (f"E5{k}: chats with 429 {x['chats_with_429']}/{x['chats']} ({100 * x['share_with_429']:.1f}%), "
                    f"failures {x['failures']}, p50 s {fmt(x['p50'], 3)}, p95 s {p95}")
            if x["import"]:
                line += (f", import throughput/min {x['import']['throughput_per_minute']:.2f}, import 429s "
                         f"{x['import']['rejections']}")
            meas.append(line)

    # E6 ---------------------------------------------------------------------------------------------------
    if "E6" in E:
        r = E["E6"]
        est, act = r["estimate"], r["actual"]
        cfg = EXP["E6"]
        at = {s["t"]: s for s in r["samples"]}
        s8, s34 = at.get(cfg["steady_from"]), at.get(cfg["steady_to"])
        band = [s["length"] for s in r["samples"] if cfg["steady_from"] <= s["t"] <= cfg["steady_to"]]
        steady = bool(s8 and band and all(within(x, s8["length"], TOL["length_band"]) for x in band))
        climbs = bool(s8 and s34 and s34["age"] >= 2 * s8["age"])
        check("P6.1", "E6 job 2's first result within 10% of the estimate at admission",
              within(act["first_result"], est["first_result"], TOL["estimate"]))
        check("P6.2", "E6 queue length within 15% of its t=8 value until t=34, while the oldest age at least doubles",
              steady and climbs)
        check("P6.3", "E6 job 2's last result within 10% of the estimate at admission",
              within(act["last_result"], est["last_result"], TOL["estimate"]))
        err = lambda a_, e_: None if a_ is None else 100 * (a_ - e_) / e_  # noqa: E731
        meas.append(f"E6 estimate: waiting ahead {est['waiting_ahead']}, rate/s {est['mu_per_second']:.3f}, "
                    f"first s {est['first_result']:.2f}, last s {est['last_result']:.2f}")
        meas.append(f"E6 actual: first s {fmt(act['first_result'], 2)} (error % {fmt(err(act['first_result'], est['first_result']), 1)}), "
                    f"last s {fmt(act['last_result'], 2)} (error % {fmt(err(act['last_result'], est['last_result']), 1)}), "
                    f"job 2 done {act['job2_done']}, 429s {r['rejections']}")
        if band:
            meas.append(f"E6 t=8..34: length min {min(band)} max {max(band)}, at t=8 {s8['length'] if s8 else 'none'}; "
                        f"oldest age s at t=8 {fmt(s8['age'] if s8 else None, 2)}, at t=34 {fmt(s34['age'] if s34 else None, 2)}")

    # E7 ---------------------------------------------------------------------------------------------------
    if "E7" in E:
        r = E["E7"]
        x, y = r["X"], r["Y"]
        want_counter = 200 if negative else 210
        tag = " [NEGATIVE CONTROL: expectation changed on purpose]" if negative else ""
        check("P7", f"E7 X: counter {want_counter}, items done 200, model calls 210{tag}",
              x["counter"] == want_counter and x["items_done"] == 200 and x["model_calls"] == 210,
              f"counter {x['counter']} ({x['counter_percent']}%), items {x['items_done']} ({x['items_percent']}%), calls {x['model_calls']}")
        check("P7", "E7 Y: items done 200, model calls 205, counter 205",
              y["items_done"] == 200 and y["model_calls"] == 205 and y["counter"] == 205,
              f"counter {y['counter']}, items {y['items_done']}, calls {y['model_calls']}")

    # E8 ---------------------------------------------------------------------------------------------------
    if "E8" in E:
        r = E["E8"]
        l1, l2, l3, l4 = (r[str(k)] for k in (1, 2, 3, 4))
        check("P8", "E8 level 1 (flag only): all 100 remaining documents complete after the cancel",
              l1["completed_after"] == 100)
        a2 = l2["at_cancel"]
        check("P8", "E8 level 2 (stop feeding): completes exactly those in flight and waiting at the cancel, fewer than level 1",
              l2["completed_after"] == a2["in_flight"] + a2["waiting"] + a2["handed_not_started"]
              and l2["completed_after"] < l1["completed_after"])
        check("P8", "E8 level 3 (skip queued): completes exactly those in flight, starts none",
              l3["completed_after"] == l3["at_cancel"]["in_flight"] and l3["started_after"] == 0)
        a4 = l4["at_cancel"]
        check("P8", "E8 level 4 (cancel in flight): completes none, starts none, one cancellation per call in flight, "
                    "partial output below 600 each",
              l4["completed_after"] == 0 and l4["started_after"] == 0 and l4["cancellations_seen"] == a4["in_flight"]
              and all(p < 600 for p in l4["partial_output_tokens"]))
        check("P8", "E8 input plus output tokens after the cancel decrease strictly from level 1 to 4",
              l1["tokens_after"] > l2["tokens_after"] > l3["tokens_after"] > l4["tokens_after"])
        for x in (l1, l2, l3, l4):
            a_ = x["at_cancel"]
            meas.append(f"E8 level {x['level']}: at cancel in flight {a_['in_flight']}, waiting {a_['waiting']}, handed "
                        f"{a_['handed_not_started']}; after: started {x['started_after']}, completed {x['completed_after']}, "
                        f"input tokens {x['input_tokens_after']}, output tokens {x['output_tokens_after']}, cancellations "
                        f"{x['cancellations_seen']}, partial output {sum(x['partial_output_tokens'])}")

    # N1 ---------------------------------------------------------------------------------------------------
    if "N1" in E:
        ceil, _ = ceiling_per_minute(task("tag_article"))
        for r in E["N1"]:
            check("N1", f"no-limit stand-in C({r['N']}): throughput within 5% of {6 * r['N']}/min (no plateau)",
                  within(r["throughput_per_minute"], 6 * r["N"], TOL["throughput"]))
            meas.append(f"N1 C({r['N']}) no limit: throughput/min {r['throughput_per_minute']:.2f}, 429s {r['rejections']}")
        plateau_passes = all(within(r["throughput_per_minute"], ceil, TOL["throughput"]) for r in E["N1"])
        check("N1", "P2.2's plateau check, applied to the no-limit numbers, fails (as it must)", not plateau_passes)
    return stable, meas


def validity(obs):
    cal = obs["calibration"]["sleep_50ms_overshoot_s"]["median"]
    ok_cal = cal <= TOL["max_sleep_overshoot_seconds"]
    fi, fc = obs["freeze_inputs"], obs["freeze_code"]
    return ok_cal, [f"Calibration: {json.dumps(obs['calibration'], sort_keys=True)} -> "
                    f"{'valid' if ok_cal else 'INVALID (median sleep overshoot above 5 ms)'}",
                    f"Freeze (inputs): matches={fi['matches']} frozen_at={fi['frozen_at_utc']} changed={fi['changed']}",
                    f"Freeze (code): matches={fc['matches']} frozen_at={fc['frozen_at_utc']} changed={fc['changed']}"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--negative-control", action="store_true")
    args = ap.parse_args()
    run = Path(args.run)
    obs = json.loads((run / "observations.json").read_text())
    stable, meas = evaluate(obs, args.negative_control)
    ok_cal, valid = validity(obs)
    digest = hashlib.sha256("\n".join(stable).encode()).hexdigest()
    head = [f"Run: {run.name}" + ("   NEGATIVE CONTROL (P2.2 and P7 changed on purpose)" if args.negative_control else ""),
            f"Started {obs['started_utc']}, finished {obs['finished_utc']}",
            f"Environment: {json.dumps(obs['environment'], sort_keys=True)}",
            f"Seconds per experiment: {json.dumps(obs['seconds'])}", *valid]
    fails = sum(line.startswith("FAIL") for line in stable)
    text = "\n".join(head + ["", "== Stable ==", *stable, "", f"Stable digest: {digest}",
                             f"Predictions: {len(stable) - fails} PASS, {fails} FAIL", "",
                             "== Measurements (vary between runs) ==", *meas]) + "\n"
    print(text, end="")
    name = "negative-control.txt" if args.negative_control else "report.txt"
    (run / name).write_text(text)
    sys.exit(1 if fails or not ok_cal else 0)


if __name__ == "__main__":
    main()
