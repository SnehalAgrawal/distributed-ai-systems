# distributed-ai-systems

Runnable demos of how AI systems behave as distributed systems: queues, workers, rate limits, concurrency and backpressure. They accompany the AI engineering series on [snehalagrawal.com](https://snehalagrawal.com/articles/). Each demo is self-contained and runs locally, with no API keys, no model and no paid services, and each demo's README says what it needs.

| Demo | Article | What it shows |
|---|---|---|
| [01-ai-workload-scaling](01-ai-workload-scaling/) | [Scaling AI Workloads With Async Processing and Workers](https://snehalagrawal.com/articles/scaling-ai-workloads-with-async-processing-and-workers/) | A worker pool for a bulk import against a stand-in provider that enforces separate request, input-token and output-token limits: Little's law, where throughput stops growing and what extra workers produce instead, why a pool needs both a concurrency limit and a rate limit per provider limit, a burst against short-window enforcement, a bulk job starving interactive chat (and what a share, a workspace cap or a separate limit changes), queue length versus age with an estimate at admission, duplicate deliveries against a progress counter, and four levels of cancel. Protocol and configuration frozen before any code; three runs. Python standard library only, no model |

## Linting

The repository has one lint setup for every demo: [Ruff](https://docs.astral.sh/ruff/) for Python and [ESLint](https://eslint.org/) for JavaScript.

```bash
npm install                              # ESLint (repository root)
pip install -r requirements-dev.txt      # Ruff
npm run lint                             # both
```

## Conventions

- Every demo has a README with the exact setup that produced the numbers in its article.
- Raw run output (`runs/`) and `.env` files are not committed; the reported copies are in each demo's `results/`.
- Every number a demo produces describes that demo's stand-ins, not a real provider.
