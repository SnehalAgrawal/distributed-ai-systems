"""Run every experiment once and save the observations: python run.py   (then: python report.py runs/<stamp>)

Order: check both freeze manifests, calibrate this machine's timing, then E1-E8 and N1.
Writes runs/<UTC time>/observations.json (runs/ is not committed). Standard library only, no model, no network.

    python run.py --only E6      one experiment, for development (not a full run)
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

from common import DEMO, task
from standin import StandIn

INPUTS = ["PROTOCOL.md", "data/config.json"]
CODE = ["python/common.py", "python/standin.py", "python/pool.py", "python/experiments.py", "python/run.py",
        "python/report.py", "python/compare.py", "python/freeze.py", "python/test_pool.py"]


def sha256(rel):
    return hashlib.sha256((DEMO / rel).read_bytes()).hexdigest()


def freeze_check(manifest_name, files):
    manifest = DEMO / "results" / manifest_name
    current = {f: sha256(f) for f in files if (DEMO / f).exists()}
    if not manifest.exists():
        return {"manifest": manifest_name, "frozen_at_utc": None, "matches": None, "changed": None}
    frozen = json.loads(manifest.read_text())
    changed = sorted(f for f in files if frozen["files"].get(f) != current.get(f))
    return {"manifest": manifest_name, "frozen_at_utc": frozen["frozen_at_utc"], "matches": not changed,
            "changed": changed}


async def calibrate():
    over = []
    for _ in range(20):
        t = time.monotonic()
        await asyncio.sleep(0.05)
        over.append(time.monotonic() - t - 0.05)
    s = StandIn(None)
    calls = []
    for _ in range(20):
        t = time.monotonic()
        await s.call(task("tag_article", call_seconds=0.0))
        calls.append(time.monotonic() - t)
    return {"sleep_50ms_overshoot_s": {"median": round(statistics.median(over), 5), "max": round(max(over), 5)},
            "zero_length_call_s": {"median": round(statistics.median(calls), 6), "max": round(max(calls), 6)}}


def environment():
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=DEMO, capture_output=True, text=True).stdout.strip()
        except OSError:
            return None
    cpu = (subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
           if sys.platform == "darwin" else platform.processor())
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
            "processor": cpu, "load_average": [round(x, 2) for x in os.getloadavg()],
            "repository_head": git("rev-parse", "--short", "HEAD"),
            "demo_files_uncommitted": bool(git("status", "--porcelain", "--", "."))}


async def main(only):
    import experiments
    started = datetime.now(timezone.utc)
    obs = {"started_utc": started.isoformat(timespec="seconds"),
           "freeze_inputs": freeze_check("freeze-inputs.json", INPUTS),
           "freeze_code": freeze_check("freeze.json", CODE),
           "environment": environment(), "calibration": await calibrate(), "experiments": {}, "seconds": {}}
    for name, fn in experiments.ALL.items():
        if only and name != only:
            continue
        print(f"{name} ...", flush=True)
        t = time.monotonic()
        obs["experiments"][name] = await fn()
        obs["seconds"][name] = round(time.monotonic() - t, 1)
    obs["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    obs["environment"]["load_average_end"] = [round(x, 2) for x in os.getloadavg()]
    stamp = started.strftime("%Y%m%dT%H%M%SZ") + (f"-{only}" if only else "")
    out = DEMO / "runs" / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "observations.json").write_text(json.dumps(obs, indent=1))
    print(f"Wrote {out / 'observations.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    asyncio.run(main(ap.parse_args().only))
