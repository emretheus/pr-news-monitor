# Five-minute walkthrough

Use the six slides in [the presentation](presentation/pr-news-monitor.pptx). The same script is embedded in the slide speaker notes. Speak naturally and use the timings as a guide.

## Slide 1: PR News Monitor (0:00–0:35)

I built this for a PR team that needs to follow its company and competitors without reading the same story repeatedly. The demo tracks Equinix and Digital Realty, and the names are configurable in YAML. It shows grouped coverage with a short description and links to the original articles. I will show one story in the dashboard, then explain the main decisions and where I would improve it.

## Slide 2: How a refresh works (0:35–1:30)

A refresh starts with NewsData.io and Data Centre Magazine. I save the metadata first, then use Newspaper4k to extract full text where possible. Similarity rules group related articles, and OpenRouter creates a description and labels each article for the feeds. One article can be relevant to both companies. SQLite stores the result, and the UI displays it after publication completes. The separation is straightforward: app.py handles presentation, pipeline.py coordinates the work, and the other modules own individual responsibilities. Normal page interactions only read saved results.

## Slide 3: Key choices and trade-offs (1:30–2:25)

I chose Streamlit because this task needs a working dashboard and one deployment. SQLite gives us persistence without running another service. The trade-off is that this design assumes one app process. YAML keeps settings simple, although a settings screen would be easier for nontechnical users. TF-IDF grouping is cheap and understandable, but it can miss paraphrases or confuse similar events. I ask the LLM for the summary and relevance together, and reuse successful results when the inputs match. Pydantic validates the response structure and article IDs. It cannot prove that the summary is factually correct.

## Slide 4: When things go wrong (2:25–3:25)

I handled failures as part of the normal flow. If a publisher blocks extraction, I keep the snippet and label the limitation. If one source fails, the other can still contribute. If both fail, the previous published results remain available. Missing keys, invalid model output, or the request budget trigger a headline and keyword fallback. A shared lock prevents overlapping refreshes in one process, while normal reruns do not call APIs. Unknown dates stay unknown. Changing the tracked companies hides old classifications until a new refresh. Grouping and source timestamps still have quality limits, which I would measure with a labeled sample.

## Slide 5: What I verified (3:25–4:05)

The automated suite has 133 passing tests, locally and inside the Linux container. It covers data preservation, malformed responses, grouping examples, UI reruns, and concurrent refreshes. I also tested real sources and OpenRouter. Docker runs as a non-root user and stores SQLite in a named volume. A health check confirms that Streamlit is serving, while a real refresh checks the integrations. For the demo, I keep a completed refresh saved so provider latency does not interrupt the walkthrough. These checks demonstrate behavior. They are not a benchmark of summary or grouping accuracy.

## Slide 6: What I would improve next (4:05–5:00)

First, I would build a labeled news sample and measure grouping, relevance, and summary quality. That tells me whether better thresholds, embeddings, or a different model actually help. For production, I would add authentication, managed secrets, API usage controls, and tested backups. Scheduled monitoring needs background jobs, durable retries, and metrics for failures and cost. Multiple workers would justify PostgreSQL and shared job coordination. On the UI, I would prioritize easier settings and clearer freshness before adding more features. I would introduce FastAPI or a separate frontend when other clients or richer interactions justify those extra boundaries.

## Quick demo cue

On slide 1, open the saved company feed, switch to competitors, and expand one story. Keep a completed refresh ready. Avoid exposing `.env` or waiting for a full live refresh during the presentation.
