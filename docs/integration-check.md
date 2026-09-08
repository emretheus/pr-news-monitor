# Step 1 — integration evidence

Checked locally on 2026-09-08. These are small live smoke checks, not a completed ingestion pipeline or a claim of general extraction/classification accuracy.

## Results

| Integration | Actual check | Outcome |
|---|---|---|
| NewsData.io | Latest endpoint, English, `Equinix OR "Digital Realty"`; follow returned pagination token | Two successful responses, 10 articles each, no overlapping article IDs across these pages. First response reported 55 total results. |
| Data Centre Magazine discovery | Ordinary HTTP requests to homepage, robots.txt, sitemap index, and September article sitemap | All accessible locally. Monthly sitemap contains 26 entries, including 22 `/news/` entries and non-news content. |
| Full-text extraction | Fetch an Equinix article once, pass downloaded HTML to Newspaper4k | Extracted title, 388 words of article text, and publication time `2026-09-03T10:30:38Z`. |
| OpenRouter | Configured `openai/gpt-4o-mini`, strict JSON schema, required provider parameter support | Successful response; locally checked required keys/types. Classified the example as company relevant and competitor irrelevant. |
| Docker runtime | Check installed CLI and daemon | CLI exists; daemon is unavailable. Container build/run remains unverified. |

The OpenRouter call used 753 tokens and reported a cost of $0.0001386. This is one observed call, not a cost forecast. The configured model was taken from the local environment file; the public model catalog confirmed structured-output support.

## Implementation decisions

### NewsData.io

Use `GET https://newsdata.io/api/1/latest` with the API key, configured query, language, and opaque `page` token from `nextPage`. Verified top-level fields are `status`, `totalResults`, `results`, and `nextPage`. Useful article fields include `article_id`, `title`, `link`, `description`, `source_name`, `source_id`, `pubDate`, and `pubDateTZ`.

The free plan returns a paid-access placeholder in `content`; never treat it as full article text. Fetch the publisher URL separately.

Provider documentation lists 200 daily credits, 10 articles per credit, a 100-character query limit, and a 12-hour delay. The latest endpoint covers up to 48 hours. Our seven-day setting is a local retention/display window, not guaranteed historical backfill. A fresh installation will have less coverage. Validate query length, and bound pagination rather than attempting to exhaust results. The 25-article cap may require three pages; retain only the configured number.

Sources: [latest endpoint](https://newsdata.io/blog/latest-news-endpoint/), [free-plan constraints](https://newsdata.io/blog/pricing-plan-in-newsdata-io/). The main documentation/pricing URLs returned an unhelpful short HTML shell during this check; use the provider's accessible documentation articles plus verified response behavior.

### Data Centre Magazine

Use the [sitemap index](https://datacentremagazine.com/sitemap.xml) to discover the monthly article sitemap(s) intersecting the recent window, including the previous month when needed. Filter article paths and exclude event/listing entries. Bound candidate inspection and extraction; this is supplementary sampled coverage, not an exhaustive crawl.

The monthly sitemap does not provide publication timestamps for the inspected entries. Read publication metadata from the article HTML; do not infer publication time from sitemap position. Exact company names in URL slugs can prioritize candidates, but must not be the sole relevance test because that would miss indirect mentions.

The site's [robots.txt](https://datacentremagazine.com/robots.txt) advertises its sitemap and disallows search and several other paths. Public news URLs are accessible without browser automation in this environment. Do not add protection-bypass dependencies speculatively. Recheck behavior inside Docker and, if attempted, Community Cloud.

Verified extraction sample: [Equinix and CPP Investments acquisition coverage](https://datacentremagazine.com/news/why-equinix-cpp-investments-have-acquired-atnorth). HTML exposes Open Graph title/description and `article:published_time`. It has no ordinary `article` element in the inspected page, so relying only on `article p` selectors would fail.

### Extraction and runtime

Use Python 3.12 (local isolated environment: 3.12.13) and Newspaper4k 0.9.6. Installation succeeded without extra system packages on this machine. Docker installation still needs verification.

Verified usage: construct `Article(url, language="en", fetch_images=False)`, supply already fetched HTML through `download(input_html=html)`, then call `parse()`. This allows the application to control retrieval and avoids an additional article download. Do not call the library's NLP method: it is unnecessary because grouping and summaries have their own stages. The library warns that optional NLTK features are unavailable; basic extraction still succeeded.

Reference: [Newspaper4k documentation](https://newspaper4k.readthedocs.io/en/latest/).

### OpenRouter

Use the ordinary HTTP chat completions endpoint with bearer authentication. Keep the model configurable. The smoke check used `response_format.type=json_schema`, strict schema validation, and `provider.require_parameters=true`.

The application will need a richer schema with explicit article IDs and multi-label relevance. The tiny smoke schema verified connectivity and structured output only. Continue validating responses locally, including article identity, and retain fallbacks. One correct classification does not establish model quality.

References: [quickstart](https://openrouter.ai/docs/quickstart), [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), [live model catalog](https://openrouter.ai/api/v1/models).

## Workspace changes and remaining work

- Added `.gitignore` before using local credentials, and a secret-free `.env.example`. Actual credentials stay in the ignored `.env` file.
- Created an ignored `.venv` for the extraction check. Application dependencies will be declared in step 2.
- Kept downloaded article HTML and API responses in an OS temporary directory, outside the repository. No raw credentials or response dumps are committed.
- Step 1 is complete. Next is configuration, typed data structures, and SQLite persistence, subject to the agreed confirmation checkpoint.
- Start the Docker daemon before step 7. Public source access and package installation on macOS do not prove the Docker/cloud deployment works.

## Step 3 — implemented ingestion smoke check

Ran the actual `pipeline.ingest_news` function against a temporary SQLite database with a three-article cap per source and a 25-second budget per source. Credentials came from the ignored local environment file; no LLM call was needed.

- First ingestion took approximately 14 seconds and saved six articles, three from each integration. All six yielded full text (316–793 words).
- The magazine selection included the verified Equinix article. Generic industry candidates are also saved; relevance analysis is a later stage.
- Both source outcomes correctly reported partial/limited coverage because the deliberately small caps prevented exhaustive discovery.
- A second ingestion reused all six successful extractions and performed zero new extractions. It still checked discovery for new metadata.
- Two NewsData.io articles had identical titles at different URLs. They remain separate records and are candidates for grouping in step 4.
- Controlled tests cover extraction failure and partial source outage; the live six-article sample happened to have no extraction failures. This sample does not establish a general success rate.
- The live check used the application's validated-IP HTTP path, with TLS verification enabled. Docker/cloud behavior remains unverified.

## Step 4 — grouping evidence

Ran ingestion with six articles per source, then grouped the recent SQLite window. All 12 articles yielded text. Grouping took approximately 0.014 seconds and produced 11 groups: the matching real-estate coverage from Ticker Report and Watch List News shared one group, while the other ten articles remained singletons. Source articles and publisher identities were retained.

Targeted examples additionally verified acquisition headlines with differing wording, the contribution of full text, unrelated events at the same company, missing/distant dates, repeated boilerplate, deterministic ordering, group limits, and a bridge that resembles two otherwise unrelated events. An extra negative example revealed similar dividend headlines from different companies could merge after name tokens were removed; a tested company-overlap guard now prevents that case.

The small live sample is useful smoke evidence, not a precision/recall benchmark. An article title referencing 2022 had a provider timestamp in 2026, exposing a source-date quality limitation documented in the README. No grouping threshold can repair incorrect input timestamps by itself.

## Step 5 — full refresh and analysis reuse

Ran the actual `pipeline.refresh_news` entry point with three articles per source, the configured OpenRouter model, and a temporary SQLite database. The first run finished in approximately 9.7 seconds: six extracted articles, five groups, five model requests, five valid analyses, and no analysis fallbacks.

The labels included an Equinix-only acquisition story, two stories relevant to both tracked companies, and two industry stories relevant to neither. The matching two-publisher coverage remained grouped and its member article IDs were validated individually. These observations are a small functional sample, not a measured classification-quality score.

The second full refresh reused all six extracted articles and all five successful analyses, making zero further model requests. Both runs reported partial coverage because the small source caps were intentionally reached; analysis itself succeeded. Snapshot publication, API-failure fallbacks, malformed output, and preservation of prior results are additionally covered by deterministic tests.

## Step 6 — dashboard and live browser refresh

Started `app.py` with Streamlit and used the dashboard's Refresh news button with the unchanged default configuration. The button disabled during work and progress updated. The refresh finished in approximately 61 seconds: NewsData.io returned 25 articles and reported its cap, while Data Centre Magazine returned 22 articles. The published snapshot contained 38 groups: 20 successful analyses and 18 explicit fallbacks after the request budget. Both the Equinix feed and competitor feed displayed nine relevant stories; memberships can overlap.

Browser inspection verified readable story cards, an expanded two-publisher story with original descriptions and article links, source status, and visible fallback labels. A rendering issue with apostrophes was corrected and covered by a regression assertion. Reloading displayed the saved snapshot without starting another refresh.

All 133 automated tests pass. Dashboard tests cover initial/rerun behavior without network calls, shared stories in both feeds, unknown dates, snippet/headline fallbacks, failed-refresh retention, configuration changes, button disabling, and one refresh per click. Threaded pipeline tests reject a simultaneous refresh and verify lock release, including database-startup failure. Ruff and dependency compatibility checks pass. The only test warning concerns unused optional Newspaper4k NLTK features.

Restarted the Streamlit process and confirmed both nine-story feeds survived without another refresh; switching to the competitor tab displayed saved coverage. This verifies local Streamlit execution. Docker and Streamlit Cloud deployment remain unverified and outside step 6.

## Step 7 — Docker verification

Built and started the image with Compose on Docker Engine 29.3.1 using Python 3.12.13 on Linux ARM64. The non-root app UID and named volume directory owner both equal 10001. Streamlit's health endpoint returned `ok`, and Compose reported a healthy container. The image excluded `.env`, Streamlit secrets, Git metadata, and local development state. Dependency compatibility checks passed.

All 133 tests passed in a disposable container using the built runtime image. Tests were mounted read-only and test dependencies installed in a temporary directory, leaving the deployed image unchanged.

The browser-triggered refresh was interrupted when its session closed. A fresh invocation of the same pipeline inside the container completed in 59.2 seconds. NewsData.io returned 25 articles and reported its cap; Data Centre Magazine returned 23 articles. The snapshot contained 39 groups, with 20 successful AI analyses and 19 budget fallbacks. Both feeds contained nine stories. A diagnostic print after completion referenced a nonexistent result attribute; independent storage inspection confirmed successful publication.

Recreated the app with `docker compose up -d --force-recreate --wait`. Compared the saved snapshot timestamp and all 39 story IDs before and after recreation: unchanged. The named volume persisted and the app remained healthy. This verifies local Linux ARM64 deployment; other architectures and public hosting were not tested.

The build initially stalled in Docker Desktop's credential helper. A temporary Docker client configuration with anonymous public registry access resolved it without changing saved credentials. Standard Compose commands worked for subsequent container operations. Verification used `NEWS_MONITOR_PORT=8502` to preserve the separate local app on port 8501.

The required deployment deliverables are complete. Streamlit Cloud remains optional and has not been deployed. The short write-up, six-slide presentation with speaker notes, and timed walkthrough are under `docs/`.

## Optional industry feed

Added explicit per-article industry labels and optional YAML sector configuration. A transactional startup migration adds the new label with a false default while retaining old snapshots and articles. The new prompt and configuration fingerprints trigger fresh analysis rather than treating old unclassified stories as industry news.

All 140 tests pass locally and inside the rebuilt Linux container. New coverage includes legacy database migration, optional sector validation, invalid/missing model labels, industry keyword fallbacks, industry-only stories, overlapping feeds, unrelated exclusions, and UI reruns without extra requests.

A live refresh against the existing Docker volume published 39 groups: 20 successful analyses and 19 budget fallbacks. The company and competitor feeds each showed nine stories; the industry feed showed 38, including 23 in neither company feed. Browser inspection confirmed the third tab, sector description, and saved results. These counts demonstrate the integration, not classification accuracy. Keyword fallbacks can over-classify incidental sector mentions; discovery remains bounded and mainly depends on magazine coverage for general industry news.
