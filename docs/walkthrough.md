# Five-minute walkthrough

Use the eight slides in [the HTML presentation](presentation/pr-news-monitor.html); the [PowerPoint deck](presentation/pr-news-monitor.pptx) mirrors the same narrative and embeds this script in its speaker notes. Speak naturally and use the timings as a guide. Slide 8 is a backup — only open it if numbers come up.

## Slide 1: PR News Monitor (0:00–0:30)

I built this for a PR team that needs to follow its company and competitors without reading the same story repeatedly. The demo tracks Equinix and Digital Realty, and the names are configurable in YAML. It shows grouped coverage with a short description and links to the original articles. I will show one story in the dashboard, then explain the main decisions and where I would improve it.

## Slide 2: The complete flow (0:30–1:00)

This is the full implementation path: discovery from NewsData.io and Data Centre Magazine, full-text extraction, persistence, grouping, LLM analysis, and dashboard publication. The industry feed reuses the same saved articles and analysis flow — no separate pipeline.

## Slide 3: One flow, clear boundaries (1:00–1:50)

A refresh runs four stages: discover, extract, group plus analyze, publish. The separation is straightforward: app.py handles presentation, pipeline.py coordinates the work, and the other modules own individual responsibilities. Normal page interactions only read saved results and never call the APIs.

## Slide 4: Stack decisions (1:50–2:40)

Each choice was cheap on purpose and has a defined prod exit. Streamlit becomes FastAPI plus a frontend when other clients need it. SQLite plus a process lock becomes PostgreSQL with queued jobs and shared coordination for schedules and replicas. TF-IDF grouping gets an embeddings verifier only if a labeled sample proves misses. The mini model plus fallback gets re-picked from measured factuality, latency, and cost. YAML becomes a settings UI when non-technical editors need it. One container gains auth, managed secrets, backups, and alerts before public use. Nothing gets rewritten until its trigger fires.

## Slide 5: When things go wrong (2:40–3:30)

I handled failures as part of the normal flow. If a publisher blocks extraction, I keep the snippet and label the limitation. If one source fails, the other can still contribute. If both fail, the previous published results remain available. A missing key, invalid model output, or an exhausted budget triggers a headline and keyword fallback — trying a second model first when one is configured. A shared lock prevents overlapping refreshes in one process. Unknown dates stay unknown. Changing the tracked companies hides old classifications until a new refresh. Grouping and source timestamps still have quality limits, which I would measure with a labeled sample.

## Slide 6: What I verified (3:30–4:10)

The automated suite has 145 passing tests, locally and inside the Linux container. It covers data preservation, malformed responses, grouping examples, two-model fallback, UI reruns, and concurrent refreshes. I also tested real sources and OpenRouter. Docker runs as a non-root user and stores SQLite in a named volume that survived container recreation. A health check confirms that Streamlit is serving, while a real refresh checks the integrations. For the demo, I keep a completed refresh saved so provider latency does not interrupt the walkthrough. These checks demonstrate behavior. They are not a benchmark of summary or grouping accuracy.

## Slide 7: What I would improve next (4:10–5:00)

First, I would build a labeled news sample and measure grouping, relevance, and summary quality. That tells me whether better thresholds, embeddings, or a different model actually help. For production, I would add authentication, managed secrets, API usage controls, and tested backups. Scheduled monitoring needs background jobs, durable retries, and metrics for failures and cost. Multiple workers would justify PostgreSQL and shared job coordination. On the UI, I would prioritize easier settings and clearer freshness before adding more features. I would introduce FastAPI or a separate frontend when other clients or richer interactions justify those extra boundaries.

## Slide 8: Backup — numbers (only if asked)

Every number is a budget: 25 articles per source, 7-day window, 60-second source budgets; 2MB pages, 15 seconds per article, 50-word minimum; 200 articles grouped, 8 per story, 72-hour date gap, 0.42 similarity; 4k characters per article, 20 analysis calls in 90 seconds, 2 models, 80-word summaries. Exceeding any of them yields partial results with labeled fallbacks — never silent failure.

## Quick demo cue

On slide 1, open the saved company feed, switch to competitors, and expand one story. Keep a completed refresh ready. Avoid exposing `.env` or waiting for a full live refresh during the presentation.
