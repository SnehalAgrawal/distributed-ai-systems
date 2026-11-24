# Protocol

Written on 2026-11-24, before any code for this demo existed. `results/freeze-inputs.json` records the SHA-256 of this file and of `data/config.json`, taken before the first line of code was written. `results/freeze.json` records the code's hashes, taken before the first reported run. Every run checks both. If anything here has to change after a freeze, the README records it under "Deviations"; this file is not edited.

Development runs (single experiments, and full runs used to debug the report code) were allowed before the code freeze. They are not reported.

The predictions below are the experiment contract approved in the article's Stage A proposal (2026-11-24), made specific enough to check. Where a prediction comes word for word from that proposal, it is marked **[Stage A]**. A prediction the results contradict is reported as FAIL and discussed. It is never adjusted after seeing the output.

## Question

A bulk AI job (a help-center import: one model call per document) runs on a worker pool against a hosted model's rate limits. With no model and no network, what can a simulation show about:

1. how throughput, calls in flight and call time relate (Little's law);
2. where throughput stops growing as workers are added, and what the extra workers produce instead;
3. why a pool needs both a concurrency limit and a rate limit, one for each of the provider's limits (requests, input tokens, output tokens);
4. what a burst at the start of a job does against a limiter that enforces short windows;
5. what a bulk job does to interactive chat on the same limits, and what a share, a provider-side cap, or a separate limit changes;
6. what queue length and the age of the oldest message each say about a backlog, and how accurate an estimate made at admission is;
7. what duplicate deliveries do to a progress counter and to per-item state;
8. how much work continues after a job is cancelled, at each level of cancelling.

## What is simulated, configured, measured, derived and documented

- **Simulated:** the provider. `standin.py` is a Python object in the same process as the pool, called with `await`, not an HTTP server. A rate-limited call returns status 429 with headers; a client that goes away is a cancelled call, and asyncio delivers the cancellation into the stand-in's coroutine, which records it. Retries, timeouts and what the network adds are #37's demo (`production-ai-patterns/05-retries-timeouts`), not this one.
- **Configured:** every limit, call time, workload size, share and schedule, in `data/config.json`, and the stand-in's rules below. The stand-in's limiter is written to follow what OpenAI's and Anthropic's documentation describes (token buckets that refill continuously, separate request, input-token and output-token limits, short-window enforcement, rejected requests that still count). It is not either provider's limiter. **Every measured number describes this stand-in and this pool, not OpenAI or Anthropic throughput.**
- **Measured:** call start and end times (`time.monotonic()`), statuses, the stand-in's bucket levels and 429 reasons, calls in flight, queue length and age, per-item status, and tokens admitted, generated and cancelled.
- **Derived:** predictions computed from the configuration (ceilings, Little's-law sizes, estimates).
- **Documented:** provider behaviour quoted in the article from the providers' pages, with the date checked. The demo doesn't test it.
- **Not in the demo (documented only):** provider batch APIs and background mode (a simulation of them would only be a simulation), `max_tokens` accounting and cached-input accounting (a stand-in configured to count them would only show what it was configured to do), and ramp limits.

## Time scale

Everything runs ten times faster than the article's illustrative numbers. **One demo second stands for ten article seconds; an "article minute" lasts 6 demo seconds.** A 10 s model call takes 1.0 demo second. The report gives throughput in documents per article minute (documents per 6 demo seconds), so its numbers can be read next to the article's arithmetic.

## The stand-in's rules (configured)

1. **Three limits, per organisation:** requests, input tokens and output tokens per article minute (`limits` in the config). Each is a bucket that refills continuously at limit ÷ 60 per article second, up to a capacity of `window` article seconds' worth (default 10 article s = 1 demo s). Buckets start full.
2. **Admission, in this order:**
   - The request bucket must hold at least 1. Otherwise: 429, reason `requests`.
   - One unit is taken from the request bucket.
   - The input bucket must hold the request's input tokens. Otherwise: 429, reason `input_tokens`, and the request unit is not given back.
   - The output bucket must be above 0. Otherwise: 429, reason `output_tokens`, and the request unit is not given back.
   - On admission, the input tokens are taken.
   - Output tokens are taken when the call ends: all of them on completion, or what had been generated on cancellation. So the output bucket can go below 0.
3. **Rejected requests count (rule `if_available`, the default).** A request rejected for tokens still uses its request unit, following OpenAI's "unsuccessful requests contribute to your per-minute limit". A request rejected because the request bucket was empty takes nothing. **Rule `always`** (used only where stated) takes one unit from every arriving request, even when that drives the bucket below zero.
4. **429 responses** carry:
   - `retry-after`: demo seconds until the bucket that refused holds enough for this request, ignoring other waiters;
   - `x-limit-reason`: which limit refused;
   - `x-remaining-requests`, `x-remaining-input-tokens` and `x-remaining-output-tokens`.

   200 responses carry the same `x-remaining-*` headers and the usage.
5. **Calls take the configured call time W** (fixed, or uniform in a range, seeded). Output is generated evenly over W. A cancelled call records the fraction of W that had passed, and the output tokens generated by then (rounded down).
6. **Optional workspace limits** (E5d only): a second set of buckets for one API key, checked before the organisation's, following Anthropic's documented workspace limits. A rejection there takes nothing from the organisation's buckets.
7. **No-limit mode** (E1, E7, E8 and negative control N1): the stand-in admits everything.

## The pool (code under test)

- **Concurrency limit:** an `asyncio.Semaphore(N)`, held from before the call until it returns, including retries.
- **Rate limits:** one token bucket per limit, at a share of the provider's limit:
  - `requests` takes 1 per attempt;
  - `input` takes the task's input tokens;
  - `output` takes the task's expected output tokens, at the start of the call.

  Each bucket's capacity is `burst` demo seconds' worth (default 0.5 = 5 article s). Buckets start empty unless stated. Waiters are served in arrival order. Every attempt, retries included, takes from the buckets.
- **On a 429:**
  - policy `R` waits `retry-after`, then tries again;
  - policy `I` tries again after a fixed 0.01 demo s pause (a stand-in for "immediately" that keeps the CPU load sane).

  Retries continue until success or the end of the run.
- **Pool shapes** used below:
  - `C(N)`: concurrency only;
  - `RATE(share)`: buckets only;
  - `BOTH(N, share)`: both;
  - `INPUT(N, share)`: concurrency plus an input-token bucket only (the article draft's snippet).

## Workloads (configured)

| Task | Input tokens | Output tokens | Call time W (demo s) | Ceiling at default limits (per article minute) | Binding limit |
|---|---|---|---|---|---|
| `tag_article` (the import) | 4,000 | 600 | 1.0 | 100 docs (400,000 / 4,000) | input tokens |
| `long_answer` | 1,000 | 1,500 | 2.0 | 53.3 (80,000 / 1,500) | output tokens |
| `classify_title` | 200 | 50 | 0.2 | 1,000 (requests) | requests |
| `chat_turn` (interactive) | 3,000 | 300 | 0.4 | — | — |

Default limits per article minute: 1,000 requests, 400,000 input tokens and 80,000 output tokens. These are the article's illustrative numbers, not any provider's tier. Every document of a task has the same size, so each ceiling is exact. Real documents vary, and a real pool would take each one's estimated size.

In demo units, the import's ceiling is 100 per 6 s = 16.67 docs per demo second. With W = 1.0, Little's law gives the calls in flight needed to reach it: L* = 16.67 × 1.0 = 16.7.

## Calibration and tolerances

Each run first measures this machine:
- twenty `asyncio.sleep(0.05)` overshoots;
- twenty zero-length stand-in calls.

A run is valid only if the median sleep overshoot is at most 5 ms, which is 2.5% of the shortest call time (0.2 s). The report prints the calibration and marks an invalid run.

Tolerances, fixed here:
- **Throughput:** within ±5% of the prediction. ±8% where call times are random (E1 variable case).
- **Estimates:** within ±10% (E6).
- **Counts:** exact unless stated.

Throughput is measured over a window that excludes the start (stated per experiment): completions with end time in the window ÷ window length, converted to per article minute.

## Experiments and predictions

### E1: Little's law (no-limit stand-in, `tag_article`)

- `C(N)` for N = 1, 2, 4, 8; W = 1.0 fixed. Each run lasts 10 s, measured over [1.5, 9.5].
- `C(8)` with W uniform in [0.5, 1.5] (seeded, mean 1.0). Lasts 32 s, measured over [2, 32].
- **Report** throughput λ, the time-averaged calls in flight L̄, and the mean call time W̄ of calls ending in the window.
- **P1.1 [Stage A] "Throughput ≈ L / W":** λ within 5% of N / W for every fixed N, and within 8% for the variable case.
- **P1.2:** L̄ within 5% of λ × W̄ (8% for the variable case).

### E2: The ceiling (default limits, `tag_article`, W = 1.0)

- `C(N)`, policy R, for N = 8, 16, 33, 67 (about ½, 1, 2 and 4 × L*). Then `C(67)` with policy I, once under rule `if_available` and once under rule `always`.
- Each lasts 12 s, measured over [3, 12].
- **P2.1:** at N = 8 and 16 (below L*), throughput within 5% of 6 × N per article minute (48 and 96), and no 429s.
- **P2.2 [Stage A] "Throughput plateaus at the ceiling; 429s appear above L* and grow":**
  - at N = 33 and 67, throughput within 5% of 100;
  - 429s at both, with more at 67 than at 33;
  - at least 95% of the 429s have reason `input_tokens`.
- **P2.3 [Stage A] "Immediate retries lower the useful rate (rejected requests counted, as configured)":** with policy I at N = 67, useful throughput (successful calls) is more than 5% below the policy R result at N = 67. Evaluated separately under each rule.
  - *Analysis written before any code, not a change to the prediction:* under rule `if_available`, request units refill at 166.7 per demo second, ten times the rate at which input tokens for one document refill. A unit burnt by a rejected request is replaced within 6 ms, so the arithmetic suggests the useful rate stays at the plateau, and P2.3 may fail under that rule. Under rule `always`, every retry drives the request bucket further below zero, and the useful rate should collapse. Both outcomes are reported as measured.

### E2b: Each limit can bind (default limits; condition B of the Stage A approval)

- For each of `tag_article` (N = 34), `long_answer` (N = 36) and `classify_title` (N = 67), each about 2 × L*:
  - `INPUT(N, 95%)`: concurrency plus an input bucket only, the draft's snippet;
  - `BOTH(N, 95%)`: concurrency plus all three buckets.
- Policy R. Each lasts 18 s, measured over [6, 18].
- **P2b.1:** with `BOTH`, throughput within 5% of 95% of each task's ceiling (95, 50.7 and 950), and no 429s.
- **P2b.2:** with `INPUT`:
  - `tag_article` behaves like `BOTH` (95, no 429s);
  - `long_answer` gets 429s, at least 95% of them with reason `output_tokens`, and throughput within 5% of 53.3;
  - `classify_title` gets 429s, at least 95% of them with reason `requests`, and throughput within 5% of 1,000.

### E3: Two limits for a pool (default limits, `tag_article`)

- Three pools: `C(20)` (policy R), `RATE(95%)` and `BOTH(20, 95%)`. Each runs with calls 5× faster (W = 0.2) and 3× slower (W = 3.0) than the 1.0 baseline.
- Each lasts 12 s, measured over [4, 12]. Record the maximum and time-averaged calls in flight.
- **P3 [Stage A] "Concurrency alone hits 429s when calls speed up; rate alone lets calls in flight grow when they slow down; both stay bounded":**
  - `C(20)`, fast: 429s, and throughput within 5% of 100. Slow: no 429s, throughput within 5% of 6 × 20 / 3 = 40, and at most 20 in flight.
  - `RATE(95%)`, fast: no 429s, throughput within 5% of 95. Slow: no 429s, throughput within 5% of 95, more than 20 calls in flight at the maximum, and time-averaged in flight within 10% of 15.83 × 3.0 = 47.5.
  - `BOTH(20, 95%)`, fast: no 429s, throughput within 5% of 95, and at most 20 in flight. Slow: no 429s, throughput within 5% of 40, and at most 20 in flight.

### E4: Burst against short-window enforcement (`tag_article`, W = 1.0)

- The stand-in's window is 1 article second (0.1 demo s). The input bucket then holds 6,667 tokens, enough for one document and a bit.
- `BOTH(20, 95%)` with the client buckets starting full (a 0.5 s burst: 7 documents' worth of input), and then starting empty.
- Each lasts 8 s.
- **P4 [Stage A] "429s at the start only with the full bucket":**
  - starting full: at least one 429, and every 429 within the first 1.0 demo s;
  - starting empty: no 429s;
  - both: throughput over [2, 8] within 5% of 95.

### E5: The limit is shared (default limits; chat plus the import)

- **Chat:** open-loop `chat_turn` arrivals, a seeded Poisson process at 40 per article minute (6.67 per demo second), scheduled from t = 3 to t = 21. The same schedule is used in every variant.
  - Latency runs from the scheduled start to completion (open loop, as in #35).
  - On a 429, a chat waits `retry-after` and tries again, if it would still finish within 2.0 demo s of its scheduled start. Otherwise it fails.
- **Import:** `tag_article` from t = 0. Each variant lasts 23 s.
- **Variants:**
  - **a:** chat alone.
  - **b:** plus the import uncapped, `C(33)` policy R.
  - **c:** plus the import at a 60% share, `BOTH(20, 60%)`.
  - **d:** plus the import uncapped, `C(33)` policy R, on its own API key with a provider-side workspace limit at 60% of each organisation limit (rule 6).
  - **e:** plus the import uncapped, `C(33)` policy R, on a separate limit of the default size. This stands for capacity that actually exists apart from chat's, such as a batch API's separate limits or separately provisioned capacity. A second API key in the same organisation is not that: per rule 1, limits are per organisation.
  - **f:** plus the import uncapped with immediate retries, `C(67)` policy I, rule `if_available`.
- **Report**, per variant:
  - the share of chats that got at least one 429;
  - chat p50 and p95 latency;
  - chat failures;
  - import throughput over [3, 21].
- **P5 [Stage A] "Chat 429s and p95 latency: worst uncapped, protected at 60%":**
  - a: no chat 429s.
  - b: at least 25% of chats get a 429, and chat p95 is at least 1.5 × a's p95.
  - c: at most 5% of chats get a 429, chat p95 is at most 1.2 × a's p95, and import throughput is within 5% of 60.
  - d: at most 5% of chats get a 429, the import gets 429s, and import throughput is within 5% of 60.
  - e: no chat 429s, and import throughput is within 5% of 100.
  - f: the share of chats with a 429 is at least b's.

### E6: Backpressure, age and the estimate at admission (default limits, `tag_article`, W = 1.0, import `BOTH(20, 60%)`, FIFO)

- **Arrivals:**
  - Job 1: 150 documents, queued at t = 0.
  - Job 2: 150 documents, queued at t = 6.
  - Single-document uploads from other tenants: one every 0.1 s from t = 6, which matches the import's rate of 10 documents per demo second.
- The run ends at t = 36.
- Every 0.5 s, record the queue length (waiting, not started) and the age of the oldest waiting document.
- **Estimate at job 2's admission:**
  - μ̂ = documents started per second over [3, 6];
  - first result ≈ waiting ahead / μ̂ + W;
  - last result ≈ (waiting ahead + 149) / μ̂ + W.
- **P6.1 [Stage A] "Job 2's first result waits ≈ items ahead ÷ ceiling":** within 10% of the first-result estimate.
- **P6.2 [Stage A] "Age climbs while length looks steady":** from t = 8 to t = 34, queue length stays within ±15% of its value at t = 8, while the age of the oldest waiting document at t = 34 is at least twice its value at t = 8.
- **P6.3 [Stage A] "The admission estimate lands within tolerance":** job 2's last result within 10% of its estimate. The error is reported as a number, not just pass or fail.

### E7: Duplicate deliveries (no-limit stand-in, `tag_article`, W = 1.0, `C(20)`)

- A job of 200 documents. Ten documents (seeded) are delivered twice, which is 5% duplicate deliveries:
  - five are redelivered right after their first delivery, so both copies run at once;
  - five are redelivered at the end of the queue, after the first copy has finished.
- **Variant X:** every delivery calls the model. Then a job counter is incremented, and the item's status is set to `done`.
- **Variant Y:** a delivery checks the item's status first and skips the call if it is already `done`.
- **P7 [Stage A] "The counter passes 100%; per-item progress stays correct":**
  - X: counter 210 (105%), items done 200 (100%), model calls 210;
  - Y: items done 200, model calls 205 (the concurrent copies both run);
  - the counter in Y counts calls made, 205.

### E8: Cancel (no-limit stand-in, `tag_article`, W uniform in [0.5, 1.5] seeded, `C(20)`)

- A job of 200 documents. A feeder adds 20 at a time whenever fewer than 20 are waiting, as the article's snippet suggests for large jobs.
- The job is cancelled when its 100th result completes.
- **Four levels of cancel:**
  1. a flag on the job only;
  2. stop feeding new documents into the queue;
  3. also skip documents already queued but not started;
  4. also cancel calls in flight.
- **Report, after the cancel:**
  - calls started and completed;
  - input tokens admitted;
  - output tokens generated, including partial output of cancelled calls;
  - cancellations the stand-in saw;
  - calls in flight and documents waiting at the moment of the cancel.
- **P8 [Stage A] "Tokens spent after cancel: most / some / almost none; the stand-in sees disconnects":**
  1. completes all 100 remaining documents after the cancel;
  2. completes exactly the documents in flight plus those waiting at the cancel, which is fewer than 1;
  3. completes exactly the documents in flight at the cancel and starts none;
  4. completes none, starts none, and the stand-in records one cancellation per call in flight at the cancel, with partial output below a full call's 600 tokens each.

  Input plus output tokens after the cancel decrease strictly from 1 to 4.
- A cancelled call in the stand-in says what the stand-in had done by then. It says nothing about whether a real provider stops generating or billing when a client disconnects (see #37).

### Negative controls

- **N1 (each run):** E2's `C(33)` and `C(67)` against the no-limit stand-in.
  - Prediction: throughput within 5% of 6 × N (198 and 402), so no plateau.
  - The report also applies P2.2's plateau check to these numbers, and that check must FAIL. If it passes, the check can't tell a ceiling from no ceiling.
- **N2:** `report.py --negative-control` re-runs the checks with two expectations changed on purpose: P2.2 expects no 429s at N = 67, and P7 expects the counter in X to read 200. It must report those two as FAIL and exit 1.

## Repeats

Three full runs after the code freeze, on the same machine, with nothing else deliberately running.

The report's stable section is compared by SHA-256 across the runs. It holds:
- the PASS/FAIL line of every prediction;
- the counts that don't depend on timing (E7 and the configuration).

Measured numbers (throughputs, 429 counts, latencies, ages) vary from run to run and are reported as ranges across the three runs. A prediction that passes in some runs and fails in others is reported as unstable, with its numbers.
