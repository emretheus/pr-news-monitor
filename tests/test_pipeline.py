from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from monitor import pipeline
from monitor.config import load_config
from monitor.models import Article, ExtractionStatus, SourceOutcome
from monitor.sources import DiscoveryResult
from monitor.storage import get_articles, initialize_database, save_article


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "news.db"
    initialize_database(path)
    return path


def discovered(provider="newsdata"):
    return Article(
        url="https://example.com/news",
        title="Example coverage",
        publisher="Example",
        provider=provider,
        provider_id=provider + "-1",
    )


def source_results(monkeypatch, news, magazine):
    monkeypatch.setattr(pipeline, "discover_newsdata", lambda *a, **k: news)
    monkeypatch.setattr(pipeline, "discover_magazine", lambda *a, **k: magazine)


def success(article):
    return DiscoveryResult((article,), SourceOutcome(article.provider, "success", 1))


def failure():
    return DiscoveryResult(
        (), SourceOutcome("newsdata", "failed", message="API key missing.")
    )


def test_one_failed_source_does_not_prevent_other_source_ingestion(monkeypatch, db):
    incoming = discovered("magazine")
    source_results(monkeypatch, failure(), success(incoming))
    monkeypatch.setattr(
        pipeline,
        "enrich_article",
        lambda a, **k: replace(
            a, full_text="Extracted text", extraction_status=ExtractionStatus.SUCCESS
        ),
    )
    result = pipeline.ingest_news(load_config(), db, newsdata_api_key="")
    assert len(result.articles) == 1
    assert [s.status for s in result.sources] == ["failed", "success"]
    assert get_articles(db, [incoming.id])[0].full_text == "Extracted text"


def test_repeated_ingestion_and_overlapping_sources_do_not_reextract(monkeypatch, db):
    source_results(monkeypatch, success(discovered()), success(discovered("magazine")))
    extract = Mock(
        side_effect=lambda a, **k: replace(
            a, full_text="Extracted text", extraction_status=ExtractionStatus.SUCCESS
        )
    )
    monkeypatch.setattr(pipeline, "enrich_article", extract)
    first = pipeline.ingest_news(load_config(), db, newsdata_api_key="secret")
    second = pipeline.ingest_news(load_config(), db, newsdata_api_key="secret")
    assert len(first.articles) == 1
    assert len(second.articles) == 1
    assert extract.call_count == 1
    assert second.reused == 2


def test_failed_extraction_can_recover_on_later_refresh(monkeypatch, db):
    incoming = discovered()
    save_article(
        db,
        replace(
            incoming,
            extraction_status=ExtractionStatus.FALLBACK,
            extraction_error="Timed out.",
        ),
    )
    source_results(
        monkeypatch,
        success(incoming),
        DiscoveryResult((), SourceOutcome("magazine", "success")),
    )
    monkeypatch.setattr(
        pipeline,
        "enrich_article",
        lambda a, **k: replace(
            a,
            full_text="Recovered text",
            extraction_status=ExtractionStatus.SUCCESS,
            extraction_error=None,
        ),
    )
    result = pipeline.ingest_news(load_config(), db, newsdata_api_key="secret")
    assert result.enriched == 1
    assert result.articles[0].extraction_error is None


def test_article_metadata_survives_unexpected_interruption(monkeypatch, db):
    incoming = discovered()
    source_results(monkeypatch, success(incoming), failure())
    monkeypatch.setattr(
        pipeline,
        "enrich_article",
        Mock(side_effect=RuntimeError("Unexpected interruption")),
    )
    with pytest.raises(RuntimeError):
        pipeline.ingest_news(load_config(), db, newsdata_api_key="secret")
    assert (
        get_articles(db, [incoming.id])[0].extraction_status == ExtractionStatus.PENDING
    )


def test_exhausted_budget_saves_metadata_without_starting_extraction(monkeypatch, db):
    incoming = discovered()
    source_results(monkeypatch, success(incoming), failure())
    monkeypatch.setattr(pipeline.time, "monotonic", Mock(side_effect=[0, 0, 2, 2, 2]))
    extract = Mock()
    monkeypatch.setattr(pipeline, "enrich_article", extract)
    result = pipeline.ingest_news(
        load_config(), db, newsdata_api_key="secret", source_budget_seconds=1
    )
    extract.assert_not_called()
    assert (
        get_articles(db, [incoming.id])[0].extraction_status == ExtractionStatus.PENDING
    )
    assert result.sources[0].status == "partial"


def test_grouping_reads_saved_recent_window_and_preserves_unknown_dates(db):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    base = discovered()
    recent = save_article(db, replace(base, published_at=now, discovered_at=now))
    save_article(
        db,
        replace(
            base,
            id="old",
            url="https://example.com/old",
            provider_id="old",
            published_at=now - timedelta(days=30),
        ),
    )
    undated = save_article(
        db,
        replace(
            base,
            id="undated",
            url="https://example.com/undated",
            provider_id="undated",
            published_at=None,
            discovered_at=now,
            title="Unrelated report",
        ),
    )
    result = pipeline.build_recent_groups(load_config(), db, now=now)
    assert result.article_count == 2
    assert not result.limited
    assert {a.id for group in result.groups for a in group.articles} == {
        recent.id,
        undated.id,
    }


def test_grouping_reports_when_saved_window_is_capped(db):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    for index in range(201):
        save_article(
            db,
            Article(
                id=str(index),
                url=f"https://example.com/{index}",
                title="",
                publisher="Example",
                provider="fixture",
                published_at=now,
            ),
        )
    result = pipeline.build_recent_groups(load_config(), db, now=now)
    assert result.limited
    assert result.article_count == 200
    assert sum(len(group.articles) for group in result.groups) == 200
