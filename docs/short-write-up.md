# PR News Monitor: decisions and next steps

## What I built

A configurable dashboard for company, competitor, and general industry coverage. It retrieves news from NewsData.io and Data Centre Magazine, extracts full text where possible, groups related articles, and uses OpenRouter for story descriptions and article relevance. Each story keeps its original coverage available for review.

`app.py` handles the Streamlit UI. `pipeline.py` coordinates ordinary Python modules for retrieval, extraction, grouping, analysis, and SQLite storage. Docker runs the app as a non-root user with a persistent database volume.

## Main trade-offs

| Choice | Reason | Limitation |
|---|---|---|
| Streamlit + SQLite | One application and simple persistence | Assumes one process and modest usage |
| YAML configuration | Easy to inspect and change | Requires a restart, less convenient for nontechnical users |
| TF-IDF grouping | Low cost and explainable rules | Can miss paraphrases or merge similar events |
| LLM summary + relevance in one call | Less latency and cost | Quality depends on the supplied evidence and model |
| Bounded retrieval and analysis | Predictable work and API usage | Coverage is a sample, and some groups use fallbacks |

Pydantic validates configuration and model responses, including article IDs. It catches malformed data, but cannot guarantee factual accuracy. Exact-input caching avoids repeating successful analysis for unchanged stories.

## Edge cases and reliability

- Blocked or failed extraction keeps metadata/snippets and shows a limitation label.
- One source can succeed independently. If all sources fail, the previous snapshot stays visible.
- Missing keys, invalid AI output, or exhausted budgets trigger headline and keyword fallbacks.
- A process-wide lock prevents overlapping refreshes. Ordinary UI reruns do not call APIs.
- Articles can belong to both feeds. Unknown dates stay unknown, and changed settings require fresh classifications.
- Industry relevance is an independent label, not a catch-all for unrelated news. It can overlap the company feeds and uses configured sector terms for keyword fallback.
- Atomic publication protects the previous snapshot if saving new results fails. The Docker volume preserves it across container recreation.

## What I would improve with more time

First, build a labeled news sample and measure grouping, relevance, and summary accuracy. Investigate misleading provider timestamps and missed coverage. Use those results to decide whether better thresholds, embeddings, or a different OpenRouter model improve quality enough to justify their cost.

For UX, prioritize clearer freshness, easier settings, and correction/history support. Keep summaries linked to evidence and preserve visible uncertainty.

## Path to production

Before public access, add authentication, usage limits, managed secrets, and tested backups. Review source access rights and content retention. Track source failures, extraction success, refresh latency, and AI cost.

Continuous monitoring needs scheduled background jobs with durable retries. Multiple workers need shared refresh coordination and a shared database such as PostgreSQL. A separate FastAPI service becomes useful for additional clients, while a richer frontend becomes worthwhile when interaction requirements outgrow Streamlit.

The current tests and live checks verify behavior, not statistical model quality. See [integration evidence](integration-check.md) and the [five-minute speaking script](walkthrough.md).
