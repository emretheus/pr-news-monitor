"""Application orchestration, independent of Streamlit."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from monitor.analysis import analysis_fingerprint, analyze_group, fallback_story
from monitor.config import AppConfig
from monitor.enrichment import enrich_article
from monitor.grouping import MAX_ARTICLES, group_articles
from monitor.models import (
    AnalysisStatus,
    Article,
    ArticleGroup,
    ExtractionStatus,
    RefreshStatus,
    SourceOutcome,
    Story,
    utc_now,
)
from monitor.sources import DiscoveryResult, discover_magazine, discover_newsdata
from monitor.storage import (
    DatabasePath,
    find_article,
    finish_refresh,
    get_snapshot,
    list_recent_articles,
    publish_stories,
    save_article,
    start_refresh,
)

_REFRESH_LOCK = Lock()


class RefreshInProgress(RuntimeError):
    """Another session already owns this process's refresh."""


def refresh_in_progress() -> bool:
    return _REFRESH_LOCK.locked()


@dataclass(frozen=True)
class GroupingResult:
    groups: tuple[ArticleGroup, ...]
    article_count: int
    limited: bool


def build_recent_groups(
    config: AppConfig,
    database: DatabasePath,
    *,
    now: datetime | None = None,
) -> GroupingResult:
    """Read the bounded recent window; leave published results untouched until analysis."""
    since = (now or utc_now()) - timedelta(days=config.news.recent_days)
    articles = list_recent_articles(database, since=since, limit=MAX_ARTICLES + 1)
    selected = articles[:MAX_ARTICLES]
    return GroupingResult(
        group_articles(selected, config), len(selected), len(articles) > MAX_ARTICLES
    )


@dataclass(frozen=True)
class IngestionResult:
    articles: tuple[Article, ...]
    sources: tuple[SourceOutcome, ...]
    enriched: int
    fallbacks: int
    reused: int


def ingest_news(
    config: AppConfig,
    database: DatabasePath,
    *,
    newsdata_api_key: str,
    source_budget_seconds: float = 60,
    progress: Callable[[str], None] | None = None,
) -> IngestionResult:
    """Give each source its own budget; persist each article before and after extraction.

    A database failure propagates: it is an application error, not a source outage.
    Initialize the database at startup before calling this function.
    """
    if not 0 < source_budget_seconds <= 120:
        raise ValueError("Source time budget must be between 0 and 120 seconds.")
    results: dict[str, Article] = {}
    outcomes = []
    enriched = fallbacks = reused = 0
    for source in ("newsdata", "magazine"):
        if progress:
            progress(f"Discovering articles from {source}.")
        deadline = time.monotonic() + source_budget_seconds
        discovery_deadline = min(deadline, time.monotonic() + 20)
        discovery: DiscoveryResult = (
            discover_newsdata(config, newsdata_api_key, deadline=discovery_deadline)
            if source == "newsdata"
            else discover_magazine(config, deadline=discovery_deadline)
        )
        errors = []
        processed = 0
        for incoming in discovery.articles:
            try:
                previous = find_article(
                    database,
                    url=incoming.url,
                    provider=incoming.provider,
                    provider_id=incoming.provider_id,
                )
                saved = save_article(database, incoming)
            except ValueError:
                errors.append("Skipped conflicting article identity.")
                continue
            # Persist rediscovered metadata and any new provider identity, but skip repeat extraction.
            if saved.id in results or (
                previous and previous.extraction_status == ExtractionStatus.SUCCESS
            ):
                reused += 1
            elif time.monotonic() < deadline:
                if progress:
                    progress(f"Reading article {processed + 1} from {source}.")
                saved = save_article(database, enrich_article(saved, deadline=deadline))
                if saved.extraction_status == ExtractionStatus.SUCCESS:
                    enriched += 1
                else:
                    fallbacks += 1
            else:
                errors.append(
                    "Extraction time budget reached; remaining metadata was saved."
                )
            results[saved.id] = saved
            processed += 1
        outcome = discovery.outcome
        if errors:
            outcome = SourceOutcome(
                source,
                "partial" if processed else "failed",
                processed,
                " ".join(filter(None, [outcome.message, *dict.fromkeys(errors)])),
            )
        outcomes.append(outcome)
    return IngestionResult(
        tuple(results.values()), tuple(outcomes), enriched, fallbacks, reused
    )


@dataclass(frozen=True)
class AnalysisBatch:
    stories: tuple[Story, ...]
    requests: int
    reused: int
    fallbacks: int
    warnings: tuple[str, ...]


def analyze_groups(
    groups: tuple[ArticleGroup, ...],
    config: AppConfig,
    database: DatabasePath,
    *,
    api_key: str,
    model: str,
    budget_seconds: float = 90,
    max_calls: int = 20,
    progress: Callable[[str], None] | None = None,
) -> AnalysisBatch:
    if not 0 < budget_seconds <= 180 or not 0 <= max_calls <= 50:
        raise ValueError(
            "Analysis requires a 0–50 call limit and a time budget of 0–180 seconds."
        )
    snapshot = get_snapshot(database, config.fingerprint)
    cached = (
        {
            story.analysis_fingerprint: story
            for story in snapshot.stories
            if story.analysis_status == AnalysisStatus.SUCCESS
        }
        if snapshot
        else {}
    )
    deadline = time.monotonic() + budget_seconds
    stories = []
    warnings = []
    requests_made = reused = 0
    stopped = False
    for index, group in enumerate(groups):
        key = analysis_fingerprint(group, config, model)
        if key in cached:
            stories.append(cached[key])
            reused += 1
            continue
        if stopped or requests_made >= max_calls or time.monotonic() >= deadline:
            stories.append(fallback_story(group, config, model))
            warnings.append(
                "Some stories use fallback analysis because the service or analysis budget was unavailable."
            )
            continue
        if progress:
            progress(f"Analyzing story {index + 1} of {len(groups)}.")
        result = analyze_group(
            group, config, api_key=api_key, model=model, deadline=deadline
        )
        stories.append(result.story)
        requests_made += int(result.request_made)
        stopped = result.stop_requests
        if result.error:
            warnings.append(result.error)
    return AnalysisBatch(
        tuple(stories),
        requests_made,
        reused,
        sum(story.analysis_status == AnalysisStatus.FALLBACK for story in stories),
        tuple(dict.fromkeys(warnings)),
    )


@dataclass(frozen=True)
class RefreshResult:
    refresh_id: int
    status: RefreshStatus
    ingestion: IngestionResult
    analysis: AnalysisBatch | None
    grouping_limited: bool = False


def refresh_news(
    config: AppConfig,
    database: DatabasePath,
    *,
    newsdata_api_key: str,
    openrouter_api_key: str,
    model: str,
    source_budget_seconds: float = 60,
    analysis_budget_seconds: float = 90,
    max_analysis_calls: int = 20,
    progress: Callable[[str], None] | None = None,
) -> RefreshResult:
    """Ingest, group, analyze, then atomically publish a complete result snapshot."""
    if not _REFRESH_LOCK.acquire(blocking=False):
        raise RefreshInProgress(
            "A refresh is already running. Please wait for it to finish."
        )
    attempt = None
    sources: tuple[SourceOutcome, ...] = ()
    try:
        attempt = start_refresh(database, config.fingerprint)
        ingestion = ingest_news(
            config,
            database,
            newsdata_api_key=newsdata_api_key,
            source_budget_seconds=source_budget_seconds,
            progress=progress,
        )
        sources = ingestion.sources
        if not sources or all(source.status == "failed" for source in sources):
            finish_refresh(
                database, attempt, status=RefreshStatus.FAILED, sources=sources
            )
            return RefreshResult(attempt, RefreshStatus.FAILED, ingestion, None)
        grouped = build_recent_groups(config, database)
        analysis = analyze_groups(
            grouped.groups,
            config,
            database,
            api_key=openrouter_api_key,
            model=model,
            budget_seconds=analysis_budget_seconds,
            max_calls=max_analysis_calls,
            progress=progress,
        )
        partial = (
            any(source.status != "success" for source in sources)
            or grouped.limited
            or analysis.fallbacks > 0
            or ingestion.fallbacks > 0
        )
        status = RefreshStatus.PARTIAL if partial else RefreshStatus.SUCCESS
        publish_stories(
            database, attempt, analysis.stories, status=status, sources=sources
        )
        return RefreshResult(attempt, status, ingestion, analysis, grouped.limited)
    except Exception:
        try:
            if attempt is not None:
                finish_refresh(
                    database, attempt, status=RefreshStatus.FAILED, sources=sources
                )
        except (sqlite3.Error, ValueError):
            pass  # A failed database or already-finished attempt must not mask the original error.
        raise
    finally:
        _REFRESH_LOCK.release()
