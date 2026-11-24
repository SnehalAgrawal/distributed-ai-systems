# 01-ai-workload-scaling

The demo for [Scaling AI Workloads With Async Processing and Workers](https://snehalagrawal.com/articles/scaling-ai-workloads-with-async-processing-and-workers/).

A help-center import runs as a bulk job: one model call per document, on a worker pool. The pool calls a **stand-in provider**, which enforces separate limits on requests, input tokens and output tokens per minute. The demo asks what the article claims:

- how throughput, calls in flight and call time relate;
- where throughput stops growing as workers are added;
- why a pool needs both a concurrency limit and a rate limit for each provider limit;
- what a burst at the start does;
- what a bulk job does to interactive chat on the same limits;
- what queue length and the age of the oldest message each say;
- what duplicate deliveries do to progress;
- how much work continues after a cancel.

**Every number here describes this stand-in and this pool, not OpenAI or Anthropic throughput.** The stand-in's limiter is configured to follow what both providers document (token buckets that refill continuously, separate request and token limits, short-window enforcement, rejected requests that still count). It is not either provider's limiter, and it reproduces nothing else about them. Provider batch APIs, background mode, `max_tokens` accounting, cached-input accounting and ramp limits are documented in the article and not simulated here.

Python 3.10 or later, standard library only. No model, no network, no API key.

## Run it

```bash
cd python
python -m unittest test_pool.py        # 14 unit tests of the stand-in's rules and the pool's pieces
python run.py                          # every experiment once, about 10 minutes; writes runs/<UTC time>/observations.json
python report.py ../runs/<UTC time>    # checks every prediction; writes report.txt next to the observations
python compare.py ../runs/<a> ../runs/<b> ../runs/<c>   # three runs: is the stable section identical? ranges of the rest
python report.py ../runs/<c> --negative-control          # two expectations changed on purpose: must FAIL and exit 1
```

`python run.py --only E6` runs one experiment, for development.

## Time scale

Everything runs ten times faster than the article's illustrative numbers. One demo second stands for ten article seconds, so an article minute lasts 6 demo seconds and a 10 s model call takes 1.0 s. Reports give throughput per article minute, so they read next to the article's arithmetic.

## What is where

| Path | What it is |
|---|---|
| `PROTOCOL.md` | The question, the stand-in's rules, every experiment and prediction, written before any code |
| `data/config.json` | Every configured value: limits, task sizes, call times, shares, schedules, seeds, tolerances |
| `python/standin.py` | The stand-in: a Python object called with `await` (not a server), three token buckets per organisation, optional workspace limits, 429s with `retry-after` and `x-remaining-*` headers, partial output recorded on cancellation |
| `python/pool.py` | The pool under test: the article's `TokenBucket`, one bucket per provider limit, a concurrency limit, the 429 policies, and a FIFO queue runner |
| `python/experiments.py` | E1–E8 and the no-limit control N1 |
| `python/run.py`, `report.py`, `compare.py`, `freeze.py` | Run, check, compare, freeze |
| `results/freeze-inputs.json` | SHA-256 of `PROTOCOL.md` and `data/config.json`, taken 2026-11-24 18:03:09 UTC, before any code existed |
| `results/freeze-v1.json`, `results/freeze.json` | SHA-256 of the code: the first freeze, taken 2026-11-24 18:20:33 UTC before the first reported run, and the second, taken 18:50:45 UTC after the `compare.py` fix (D6) |
| `results/report-run1.txt` … `report-run3.txt`, `compare.txt`, `negative-control.txt` | The reported runs |
| `python/after_freeze.py`, `results/after-freeze.txt` | Three checks added during the review, after the freeze (D7) |
| `results/observations-run1.json` | Run 1's raw observations |

## Results

### Setup

Three full runs, on 2026-11-24 from 18:20:37 to 18:50:08 UTC, each lasting 9 minutes 50 seconds:
- Python 3.12.13, standard library only;
- macOS 26.6.2 on an Apple M4 Pro, with other programs running (one-minute load average 1.43 to 2.69);
- median overshoot of a 50 ms sleep: 1.1 ms in every run, within the 5 ms the protocol allows.

`results/compare.txt` has every number's range across the runs.

### Summary

**Predictions:** 52 per run. Runs 1 and 2 passed 46 and run 3 passed 45. The stable sections of runs 1 and 2 are identical (SHA-256 `d29df5bd…`). Run 3 differs by one line, P8 level 4.

**Negative controls:**
- N1, the stand-in with no limit: throughput kept rising (198 and 402 a minute), so the plateau check fails there, as it must.
- N2, `report.py --negative-control` on run 3: both changed expectations FAIL, and it exits 1.

### Numbers

Per article minute; ranges across the three runs.

| Experiment | Result |
|---|---|
| E1 Little's law, no limit | 6.0 / 12.0 / 24.0 / 48.0 docs/min at 1 / 2 / 4 / 8 in flight; 48.4 with random call times |
| E2 ceiling (100/min), concurrency only, retry-after | 48 and 96 below L*, no 429s; 100 at 33 and 67, with 21,681–21,693 and 90,713–90,753 429s |
| E2 immediate retries at 67 | rule `if_available`: 100/min, 53,344–53,793 429s; rule `always`: 0/min |
| E2b input bucket only | tag 95 (no 429s); long answers 36 (232 429s, output tokens); classify 1,000 (2,534 429s, requests) |
| E2b three buckets at 95% | 95 / 51 / 950, no 429s |
| E3 calls 5× faster | concurrency only: 100.5, about 25,000 429s; rate only and both: 94.5, no 429s, at most 4 in flight |
| E3 calls 3× slower | concurrency only: 30 in the window (40 by Little's law), 11–14 429s at the start; rate only: 95.3, 48 in flight; both: 33.75 in the window (40 by Little's law), at most 20 in flight |
| E4 short-window enforcement | client buckets start full: 7 429s, all in the first 0.006 s; start empty: none; 95/min after the start for both |
| E5 chat with 429 / chat p95 / import | a: 0% / 0.403 s / –; b: 97–99% / 1.37–1.53 s (0–4 chats failed) / 72–73; c: 0% / 0.402–0.403 s / 60; d: 0% / 0.402 s / 60 (about 43,000 import 429s); e: 0% / 0.401 s / 100; f: 80–90% / 0.485–0.492 s / 71 |
| E6 estimate at admission | first result: estimated 10.0–10.1 s, measured 10.1 s (error 0–1%); last result: estimated 24.9–25.0 s, measured 25.0 s (error 0–0.4%); queue length 240–242 from t = 8 to 34 while the oldest age went from 8 s to 24 s |
| E7 duplicates | X: counter 210 (105%), items 200, calls 210; Y: items 200, calls 205 |
| E8 cancel, after the cancel (19 in flight, 21 waiting) | flag: 100 completed, 384,000 tokens; stop feeding: 40, 108,000; skip queued: 19, 11,400; cancel in flight: 0 completed, 19 cancellations, 6,744–6,763 partial output tokens |

Demo seconds in E5 and E6 are the stand-in's times. Multiply by 10 for the article's scale.

### Reading E6

P6.2 passed as frozen: queue length stayed at 240–242 while the oldest age went from 8 s to 24 s. That doesn't mean the wait got worse. Every single upload waited the same 24.1 s before starting, which is length ÷ throughput (D7). The age climbed because the head of the queue was job 1's burst, queued at t = 0. Once the queue had turned over, age settled at that same wait. Age measures the wait directly; length predicts it only together with throughput.

### Predictions that failed

The same six failed in all three runs:
1. **P2.2, the 429 reason:** 91% (at 33) and 98% (at 67) of the 429s were on requests, not input tokens.
   - `retry-after` tells each refused worker when there will be room for it alone, so they all come back at once.
   - Each refusal for tokens uses a request unit, until the request bucket runs dry.
2. **P2.3 under rule `if_available`:** immediate retries didn't lower the useful rate. The analysis written before any code expected this: a burnt request unit refills within 6 ms. Under rule `always`, the rate fell to 0.
3. **P2b.2, long answers with an input bucket only:** 36/min in the window, not the 53.3 ceiling.
   - The 36 is partly a window artifact: completions come in batches of 36 every 5 demo s, and the window [6, 18] catches two of them. Over longer runs, the rate is about 43/min (D7).
   - The shortfall is real. Output is charged when a call ends (the stand-in's own rule, which matches neither provider), and admission only needs the output bucket above zero. So the waiting calls start together, put the bucket 54,000 tokens into debt when they finish, and refill is lost at the cap meanwhile.
4. **P3, concurrency only, slow calls:** 30/min in the window (D1: 40 by Little's law), and 11–14 429s from the 20 calls sent at once at the start.
5. **P3, both limits, slow calls:** 33.75/min in the window (D1: 40 by Little's law).
6. **P5 variant f:** immediate retries gave fewer chats a 429 (80–90%) than the uncapped import with `retry-after` (97–99%), with a much lower chat p95 (0.49 s against 1.37–1.53 s) and no failures.
   - In b, about 96% of chat's refusals were on the request limit (D7): the `retry-after` herd from E2 drains request units each time it wakes.
   - In f, chat was refused far less often.

One more failed in run 3 only:
7. **P8, level 4:** one of the 19 cancelled calls had finished generating when the cancel reached it, so it recorded all 600 output tokens.

## Deviations

`PROTOCOL.md` was not edited after its freeze. Each deviation below is a choice made after it, recorded here.

- **D1. A Little's-law line for E3, added after the development runs and before the code freeze.**
  - **Cause:** with fixed call times, the 20 calls of a concurrency-bound pool finish in lockstep batches. A measurement window that isn't a whole number of call times then miscounts. With 3 s calls, the 8 s window catches two batches instead of 2.67.
  - **Change:** the report adds, for each E3 run, average in flight ÷ call time, which is the throughput by Little's law from the same run.
  - **Not changed:** the P3 checks still use the protocol's window. Where they fail for this reason, they are reported as FAIL.
- **D2. E5's p95 counts a chat that gave up as slower than any success.** This is an implementation choice made while writing the code, before any run. The protocol asks for p50 and p95 latency, and for failures, without saying how they combine.
- **D3. E8 counts calls already handed to a worker but not yet started.** This is an implementation choice made before any run.
  - At level 2, those calls count toward "in flight plus waiting".
  - At level 3, the worker checks the cancel before calling, so they are skipped.
  - The report prints their number; it was 0 in every run.
- **D4. 429 counts cover the whole run.** Throughput is counted inside each experiment's window, but 429 counts include the warm-up. This matters most in E3, where the concurrency-only pool's only 429s came at the very start.
- **D5. Development runs.** Single experiments and full runs were used before the code freeze to debug the code. They are not reported and were deleted. They showed the same pattern of passes and failures as the reported runs.
- **D6. `compare.py` was fixed after the three runs.**
  - **Cause:** report lines containing JSON (`{...}`) crashed its output formatting.
  - **Fix:** it now escapes braces. Nothing it measures or compares changed.
  - **Freezes:** the runs were checked against the first code freeze, kept as `results/freeze-v1.json` (18:20:33 UTC). `results/freeze.json` is the second freeze (18:50:45 UTC), in which only `python/compare.py` differs.
- **D7. Checks after the freeze.**
  - **Why:** the delegated review (a second model, not a person) questioned three explanations.
  - **What:** `python/after_freeze.py` re-runs the frozen experiment code with longer windows or extra counts. Its output is in `results/after-freeze.txt`.
  - **Findings:**
    - E2b's long answers settle at about 43/min over longer runs.
    - In E5b, chat's refusals were mostly on the request limit; E5f had far fewer.
    - In E6, every single upload waited 24.1 s.
  - **Not changed:** no frozen result or PASS/FAIL. The article and this README label these numbers "after the freeze".
