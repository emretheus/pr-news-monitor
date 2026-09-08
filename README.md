# PR News Monitor

A PR news dashboard for Equinix and its competitors, built incrementally for the coding challenge in [task.md](task.md).

**Current state:** the backend pipeline is complete through source discovery, extraction, grouping, LLM analysis, and atomic publication. The Streamlit dashboard and Docker packaging are upcoming steps. There is no running dashboard yet.

## Development setup

Use Python 3.12. From the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Alternatively, with `uv` installed:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use temporary SQLite files and require no API keys, network calls, or Streamlit session. Runtime dependencies are in `requirements.txt`; test dependencies are in `requirements-dev.txt`. Additional runtime dependencies will be added when their implementation steps begin.

For live integrations, create `.env` from `.env.example` if you do not already have one, then fill in `NEWSDATA_API_KEY`, `OPENROUTER_API_KEY`, and `OPENROUTER_MODEL`. Never commit `.env`. The future application entry point will load credentials at startup and pass them to integrations; storage and configuration imports do not read secrets or make requests.

## Configuration

Edit `config.yaml` to change the company, competitors, aliases, and retrieval limits. Restart the application after changes once the dashboard is implemented. The entity name is always included among its search terms; additional aliases are optional.

Validation rejects blank names, shared names/aliases between entities, unknown settings, unsupported languages, and invalid numeric limits. The prototype supports English, 1–10 competitors, a 1–30 day local display window, and 1–100 articles per source per refresh. Defaults are seven days and 25 articles.

The seven-day window is local retention/display filtering, not guaranteed historical coverage. NewsData.io's latest endpoint provides up to 48 hours of news with a free-plan delay. Old records are currently retained in SQLite; automatic history cleanup is deferred.

## Module boundaries

```text
monitor/
  config.py     Validate YAML and identify the active configuration
  models.py     Immutable Python data structures, statuses, fingerprints
  storage.py    SQLite schema, queries, and transactions
  http.py       Bounded public HTTP retrieval and URL normalization
  sources.py    NewsData.io and magazine sitemap discovery
  enrichment.py Full-text extraction with metadata/snippet fallback
  grouping.py   Deterministic lexical grouping of recent articles
  analysis.py   OpenRouter schema, bounded context, validation, and fallback
  pipeline.py   Full refresh orchestration, analysis reuse, and publication
tests/
  fixtures/     Invented publisher HTML for extraction tests
  test_*.py     Configuration, storage, HTTP, sources, extraction, ingestion
```

The upcoming `app.py` will render the UI and call the pipeline. It will contain no SQL, source retrieval, grouping rules, or LLM prompts. Modules under `monitor/` do not import Streamlit. See [plan.md](plan.md) for the complete target structure and remaining steps.

## Persistence decisions

- **Concrete functions, standard-library SQLite:** no ORM or generic repository layer. Each operation opens and closes its own connection, enables foreign keys, and uses a transaction. This avoids sharing a SQLite connection across UI threads.
- **Article identity:** normalized URLs are unique. A small provider-identity table maps multiple discovered IDs to the same article. If a provider ID and URL contradict existing records, storage rejects that observation instead of silently merging coverage. Ingestion removes fragments and known tracking parameters while preserving meaningful query values and their order. Different URLs with identical titles remain separate articles for story grouping.
- **Preserve useful data:** rediscovery keeps the internal article ID and earliest discovery time. Missing metadata or failed extraction cannot erase existing useful text. Supplied corrections update the content fingerprint. Articles require timezone-aware dates, stored in UTC; unknown publication dates stay unknown.
- **Separate attempts from results:** refresh attempts record source outcomes. Published story snapshots record the latest complete results for a particular configuration. A failed attempt does not replace the last snapshot.
- **Atomic publication:** replace story membership, analysis, snapshot timestamp, and refresh status in one transaction. An invalid member or write failure rolls everything back. An older refresh cannot overwrite a newer published result. The application-wide refresh guard comes with the pipeline/UI steps.
- **Explicit membership:** an article belongs to one story per configuration. Its company and competitor labels are independent booleans, allowing both or neither. A story belongs to a feed if any member is relevant to it.
- **Configuration isolation:** fingerprints include tracked entities, aliases, and news settings. Reads for a changed configuration do not accidentally show old classifications. Formatting and ordering changes do not invalidate equivalent settings.

SQLite is intended for one application process with bounded refreshes. A PostgreSQL move would require deliberate schema/query work in `storage.py`, but processing and UI code would keep using plain data. Database schema migration tooling and multi-process refresh coordination are intentionally deferred.

## Verification so far

Focused tests cover configuration errors, duplicate and conflicting identities, preservation of extracted content, UTC/missing dates, changed content fingerprints, story labels, failed refreshes, transaction rollback, configuration isolation, and stale refresh publication.

HTTP/source tests also cover unsafe destinations and redirects, response size limits, bounded retries, repeated pagination tokens, malformed responses, sitemap month boundaries, parser network isolation, and metadata preservation after interrupted ingestion.

Live evidence is recorded in [docs/integration-check.md](docs/integration-check.md). The full refresh fetched three articles per source, extracted all six, formed five groups, and generated five validated LLM analyses. The next refresh reused all six extractions and all five analyses, making zero additional LLM calls.

## Ingestion decisions and limits

- Call `pipeline.ingest_news(config, database, newsdata_api_key=...)` after initializing storage. It returns articles, separate source outcomes, and extraction/reuse counts. The optional progress callback receives plain text; no UI framework is involved.
- Each source gets its own 60-second budget by default, with discovery limited to 20 seconds and individual extraction requests to 15 seconds. This prevents one source from consuming the other's entire allocation. System DNS and local HTML parsing are not forcibly interruptible; these are operational budgets, not hard real-time guarantees.
- HTTP reads stop after 2 MB, use short socket timeouts, allow at most three checked redirects, and retry transient failures once. Long rate-limit delays are reported instead of blocking. Credential-bearing NewsData.io requests never follow redirects.
- The shared HTTP module rejects non-public destinations and connects to the validated IP while retaining the original TLS hostname and certificate verification. It does not use environment proxies. Errors omit request URLs, response bodies, and secrets. A single chosen public address may fail even when another address works; address failover is deferred.
- NewsData.io queries are split at 100 characters and pages are capped. Invalid metadata is skipped. Its gated `content` field is never used as article text.
- Magazine discovery inspects monthly sitemaps covering the configured window and accepts public news/article paths. Explicit company names in slugs are prioritized, but other candidates remain eligible for indirect mentions. Sitemap entries lack reliable publication dates, so dates are read during enrichment. This bounded selection is a sample and may miss relevant coverage; recent-window filtering is applied when reading saved articles for grouping.
- Newspaper4k parses already downloaded HTML. Image fetching, embedded refresh redirects, and read-more fetching are disabled. Non-HTML responses, very short text, common challenge titles, or obvious title/text mismatches use metadata/snippets instead. These checks cannot guarantee that every extracted page is clean.
- Save metadata before extraction and save each result immediately. A source outage does not discard the other source's results. Previously successful text is reused; failed extraction can retry on a later explicit refresh, but overlapping sources do not fetch the same article twice in one run. Successful cached full text is not periodically re-fetched for publisher corrections yet.
- Hitting a configured cap is reported as limited coverage. Extraction failures are counted separately from source discovery status. If extraction time runs out, remaining metadata stays saved with extraction pending.
- Newspaper4k emits a warning about optional NLTK features; we do not install or use those NLP features. Text extraction works without them.

## Story grouping decisions

Call `pipeline.build_recent_groups(config, database)` after ingestion. It reads at most 200 recent saved articles, reports whether that window was capped, and returns candidate groups. This function does not publish a dashboard snapshot; the full refresh adds descriptions and relevance labels before publishing.

`grouping.group_articles` is a pure processing function: no network or SQLite access. It uses scikit-learn's TF-IDF implementation rather than custom vectorization code. The additional numerical dependencies increase installation size, but avoid maintaining our own text similarity implementation.

1. Sort articles deterministically, with known publication dates first and oldest coverage first. Each group's first article is its fixed representative.
2. Calculate separate cosine similarities for titles and the first 600 words of full text (or snippets). Combine them with 65% title weight and 35% body weight. Remove English stop words and configured company-name tokens so a shared company name alone cannot cause a merge.
3. Require some title agreement, a combined score of at least 0.42, and at most 72 hours between the candidate and representative. Missing publication dates use discovery time only for this internal comparison and require a stricter 0.65 score; they remain visibly unknown dates.
4. Reject a match if both articles mention tracked companies but their company sets do not overlap. This keeps similarly worded announcements from different companies apart. Articles mentioning both can still join related coverage.
5. Assign the article to its best qualifying representative, or create a singleton. Do not merge groups through transitive chains. Keep at most eight articles per group; additional coverage becomes another group rather than disappearing.
6. Derive group IDs from sorted member article IDs. Display groups newest first, with entirely undated groups last. Reordering the same input does not change memberships or IDs.

These are initial heuristic thresholds, checked against targeted examples and a small live sample, not a statistically calibrated classifier. The tests demonstrate that full text can join differently worded acquisition coverage, while unrelated company events, boilerplate, distant dates, and bridging articles stay separate. A discovered false merge between different companies' dividend announcements motivated the company-overlap guard.

Limitations: lexical similarity can miss paraphrases and can merge similarly templated events with different details. Broad representative articles can still attract overly broad coverage. Removing name tokens can discard useful context; the company-overlap guard can split reports that omit the other party. Rebuilding with new articles changes TF-IDF weights and may change memberships. Eight-member limits may split heavily covered events. Evaluate a labeled news sample before adding embeddings, entity extraction, or a second-stage verifier.

The live sample also included an article whose title referenced 2022 while the provider supplied a 2026 publication timestamp. Grouping currently trusts supplied dates; reconciling original publisher dates with provider indexing timestamps needs a separate improvement. Do not interpret the recent-window filter as proof that every event itself occurred recently.

## LLM analysis and full refresh

`pipeline.refresh_news` now connects ingestion, grouping, analysis, and publication. Pass the validated configuration, initialized database path, NewsData.io key, OpenRouter key, and configured model explicitly. It returns a `RefreshResult` containing source outcomes, extraction counts, analysis requests/reuse/fallback counts, and the final status. A progress callback can display its messages in the upcoming UI.

- **One request per changed story:** OpenRouter receives a factual-description request and relevance classification for every member article in the same call. It receives up to eight articles, each with title, publisher, date, and at most 4,000 characters of full text or snippet. Titles and publisher strings are also bounded. The model does not fetch article links or invoke tools.
- **Pydantic at the response boundary:** request strict JSON-schema output, then validate it locally. Require a nonblank description, actual booleans, and exactly the supplied article IDs once each. Reject extra fields, unknown/missing/duplicate IDs, truncated generations, and malformed responses. Restore labels to the original article order before saving.
- **Independent feed labels:** each article can be company relevant, competitor relevant, both, or neither. Stored story membership aggregates those labels; the UI will filter stories into feeds. Irrelevant stories can stay saved without appearing in either feed.
- **Evidence and uncertainty:** article text is explicitly treated as untrusted evidence in the prompt. Ask for only supplied facts and preserve uncertainty when coverage differs. Validation checks structure and identity; it cannot prove a summary is factually grounded or that the model ignored every embedded instruction. Bounded excerpts can omit important details.
- **Bounded requests:** default to at most 20 LLM calls and a 90-second analysis budget per refresh; each request uses a short connection timeout and at most a 20-second read timeout. Responses are capped at 128 KB and 1,200 output tokens. No automatic POST retries or redirects; an uncertain retry could charge twice. A provider/network failure stops further uncached requests for that refresh, while existing successful cache entries can still be reused.
- **Honest fallback:** missing credentials, invalid output, service failure, or an exhausted budget produce the representative article title and conservative whole-alias matching. `analysis_status=fallback` tells the UI to label this clearly. Keyword fallback can over-classify incidental mentions and is not equivalent to model analysis.
- **Exact-input caching:** reuse only successful analyses from the latest published snapshot for the active configuration. Fingerprints include sorted article IDs, the actual bounded context sent, configuration, model, prompt version/text, and response schema. Changing relevant evidence or membership invalidates reuse. Content beyond the supplied excerpt does not affect that analysis input. Failed/fallback analyses are retried on a later refresh.
- **Atomic publication:** keep old results visible until the entire new story set is ready. If all news sources fail, skip analysis and retain the old snapshot. Unexpected application errors are recorded as failed attempts and re-raised; partial or failed database writes cannot replace a complete snapshot.

The analysis cache deliberately reuses the existing snapshot tables instead of adding another cache service/table. Successful model calls made during a run that fails before publication are not retained, so a later run may repeat them. A changed configuration or group that disappears and returns may also require new analysis. The application-wide refresh guard and visible fallback/status indicators are part of step 6.

## Deployment target

Docker and Docker Compose with a persistent SQLite volume are required deliverables in step 7. They are not implemented yet. The Docker CLI is installed locally, but its daemon was unavailable during step 1.

Streamlit Community Cloud is optional for a demo after the core application and Docker checks pass. Its local database would be a rebuildable cache because local filesystem persistence is not guaranteed. Production hosting is outside this prototype's scope.
