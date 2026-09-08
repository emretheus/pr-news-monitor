import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from monitor.models import (
    AnalysisStatus,
    Article,
    ArticleRelevance,
    ExtractionStatus,
    RefreshStatus,
    SourceOutcome,
    Story,
)
from monitor.storage import (
    find_article,
    finish_refresh,
    get_articles,
    get_latest_refresh,
    get_snapshot,
    initialize_database,
    list_recent_articles,
    publish_stories,
    save_article,
    start_refresh,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "news.db"
    initialize_database(path)
    return path


def article(**changes):
    defaults = {
        "url": "https://example.com/news/acquisition",
        "title": "Equinix announces acquisition",
        "publisher": "Example News",
        "provider": "newsdata",
        "provider_id": "provider-1",
        "snippet": "A company announcement.",
        "published_at": NOW,
        "discovered_at": NOW,
    }
    return Article(**(defaults | changes))


def story(saved, **changes):
    defaults = {
        "id": "story-1",
        "description": "Equinix announced an acquisition.",
        "articles": (ArticleRelevance(saved.id, company=True),),
        "analysis_fingerprint": "analysis-v1",
    }
    return Story(**(defaults | changes))


def test_repeated_ingestion_and_startup_preserve_article(db):
    saved = save_article(db, article())
    repeated = save_article(db, article())
    initialize_database(db)
    assert repeated.id == saved.id
    assert get_articles(db, [saved.id]) == (saved,)


def test_industry_migration_preserves_old_snapshot_and_new_labels(db):
    saved = save_article(db, article())
    attempt = start_refresh(db, "config")
    publish_stories(db, attempt, [story(saved)])
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE story_articles DROP COLUMN industry")
    initialize_database(db)
    old = get_snapshot(db, "config")
    assert old.stories[0].company_relevant
    assert not old.stories[0].industry_relevant
    updated = story(saved, articles=(ArticleRelevance(saved.id, industry=True),))
    attempt = start_refresh(db, "config")
    publish_stories(db, attempt, [updated])
    initialize_database(db)
    assert get_snapshot(db, "config").stories == (updated,)
    assert len(list_recent_articles(db, since=NOW - timedelta(days=7))) == 1


def test_same_url_from_multiple_providers_keeps_all_identities(db):
    saved = save_article(db, article())
    other = save_article(db, article(provider="magazine", provider_id="magazine-1"))
    assert other.id == saved.id
    assert (
        find_article(db, url="", provider="magazine", provider_id="magazine-1").id
        == saved.id
    )
    assert (
        find_article(db, url="", provider="newsdata", provider_id="provider-1").id
        == saved.id
    )


def test_provider_id_recognizes_changed_url(db):
    saved = save_article(db, article())
    moved = save_article(db, article(url="https://example.com/news/corrected-url"))
    assert moved.id == saved.id
    assert find_article(db, url=moved.url).id == saved.id


def test_conflicting_identity_does_not_merge_or_modify_articles(db):
    first = save_article(db, article())
    second = save_article(
        db, article(url="https://example.com/other", provider_id="provider-2")
    )
    with pytest.raises(ValueError, match="different saved articles"):
        save_article(db, article(url=second.url))
    assert get_articles(db, [first.id, second.id]) == (first, second)


def test_missing_fields_and_failed_extraction_do_not_erase_saved_content(db):
    saved = save_article(
        db,
        article(
            full_text="Useful extracted article text.",
            extraction_status=ExtractionStatus.SUCCESS,
        ),
    )
    result = save_article(
        db,
        article(
            title=" ",
            publisher="",
            snippet="  ",
            full_text=" ",
            published_at=None,
            discovered_at=NOW + timedelta(hours=1),
            extraction_status=ExtractionStatus.FALLBACK,
            extraction_error="Publisher timed out.",
        ),
    )
    assert result == saved
    assert result.content_fingerprint == saved.content_fingerprint


def test_rediscovery_keeps_previous_extraction_failure(db):
    saved = save_article(
        db,
        article(
            extraction_status=ExtractionStatus.FALLBACK,
            extraction_error="Blocked by publisher.",
        ),
    )
    assert save_article(db, article()) == saved


def test_new_content_changes_fingerprint_but_not_article_identity(db):
    saved = save_article(db, article())
    updated = save_article(db, article(snippet="Corrected announcement."))
    assert updated.id == saved.id
    assert updated.content_fingerprint != saved.content_fingerprint


def test_dates_are_normalized_and_unknown_dates_stay_unknown(db):
    dated = save_article(
        db, article(published_at=NOW.astimezone(timezone(timedelta(hours=2))))
    )
    unknown = save_article(
        db,
        article(
            url="https://example.com/undated",
            provider_id=None,
            published_at=None,
            discovered_at=NOW + timedelta(hours=1),
        ),
    )
    save_article(
        db,
        article(
            url="https://example.com/old",
            provider_id="old",
            published_at=NOW - timedelta(days=30),
        ),
    )
    results = list_recent_articles(db, since=NOW - timedelta(days=7))
    assert [row.id for row in results] == [dated.id, unknown.id]
    assert results[0].published_at.tzinfo == UTC
    assert results[1].published_at is None
    with pytest.raises(ValueError, match="timezone"):
        article(published_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="limit"):
        list_recent_articles(db, since=NOW, limit=-1)


def test_snapshot_preserves_labels_analysis_status_and_order(db):
    first = save_article(db, article())
    second = save_article(
        db, article(url="https://other.example/coverage", provider_id="p2")
    )
    grouped = story(
        first,
        articles=(
            ArticleRelevance(second.id, company=True, competitor=True),
            ArticleRelevance(first.id, company=True),
        ),
        analysis_status=AnalysisStatus.FALLBACK,
    )
    refresh = start_refresh(db, "config-a", started_at=NOW)
    publish_stories(db, refresh, [grouped], published_at=NOW)
    initialize_database(db)
    snapshot = get_snapshot(db, "config-a")
    assert snapshot.stories == (grouped,)
    assert snapshot.stories[0].company_relevant
    assert snapshot.stories[0].competitor_relevant
    assert snapshot.refresh_id == refresh


def test_failed_refresh_keeps_successful_snapshot_and_records_source_status(db):
    saved = save_article(db, article())
    first = start_refresh(db, "config-a", started_at=NOW)
    publish_stories(db, first, [story(saved)], published_at=NOW)
    failed = start_refresh(db, "config-a", started_at=NOW + timedelta(minutes=1))
    outcomes = (SourceOutcome("newsdata", "failed", message="Rate limit reached."),)
    finish_refresh(db, failed, status=RefreshStatus.FAILED, sources=outcomes)
    assert get_snapshot(db, "config-a").refresh_id == first
    latest = get_latest_refresh(db, "config-a")
    assert latest.id == failed
    assert latest.status == RefreshStatus.FAILED
    assert latest.sources == outcomes


@pytest.mark.parametrize(
    "failure", ["missing_article", "duplicate_membership", "empty_story"]
)
def test_failed_publication_rolls_back_entire_snapshot(db, failure):
    saved = save_article(db, article())
    original = story(saved)
    first = start_refresh(db, "config-a")
    publish_stories(db, first, [original], published_at=NOW)
    pending = start_refresh(db, "config-a")
    invalid = {
        "missing_article": replace(
            original, id="story-2", articles=(ArticleRelevance("missing"),)
        ),
        "duplicate_membership": replace(original, id="story-2"),
        "empty_story": replace(original, id="story-2", articles=()),
    }[failure]
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        publish_stories(db, pending, [original, invalid])
    snapshot = get_snapshot(db, "config-a")
    assert snapshot.refresh_id == first
    assert snapshot.published_at == NOW
    assert snapshot.stories == (original,)
    assert get_latest_refresh(db, "config-a").status == RefreshStatus.RUNNING


def test_configuration_isolation_and_successful_empty_snapshot(db):
    saved = save_article(db, article())
    first = start_refresh(db, "config-a")
    publish_stories(db, first, [story(saved)])
    assert get_snapshot(db, "config-b") is None
    second = start_refresh(db, "config-b")
    publish_stories(db, second, [])
    assert get_snapshot(db, "config-b").stories == ()
    assert len(get_snapshot(db, "config-a").stories) == 1


def test_partial_refresh_publishes_and_finished_refresh_cannot_be_reused(db):
    saved = save_article(db, article())
    refresh = start_refresh(db, "config-a")
    publish_stories(db, refresh, [story(saved)], status=RefreshStatus.PARTIAL)
    assert get_latest_refresh(db, "config-a").status == RefreshStatus.PARTIAL
    with pytest.raises(ValueError, match="already finished"):
        publish_stories(db, refresh, [])
    with pytest.raises(ValueError, match="already finished"):
        finish_refresh(db, refresh, status=RefreshStatus.FAILED)


def test_failed_refresh_cannot_replace_stories(db):
    refresh = start_refresh(db, "config-a")
    with pytest.raises(ValueError, match="Only successful"):
        publish_stories(db, refresh, [], status=RefreshStatus.FAILED)
    assert get_snapshot(db, "config-a") is None


def test_slow_older_refresh_cannot_overwrite_newer_results(db):
    saved = save_article(db, article())
    older = start_refresh(db, "config-a")
    newer = start_refresh(db, "config-a")
    publish_stories(db, newer, [story(saved)])
    with pytest.raises(ValueError, match="newer refresh"):
        publish_stories(db, older, [])
    assert get_snapshot(db, "config-a").refresh_id == newer


def test_successful_empty_refresh_clears_old_stories_but_keeps_articles(db):
    saved = save_article(db, article())
    first = start_refresh(db, "config-a")
    publish_stories(db, first, [story(saved)], published_at=NOW)
    second = start_refresh(db, "config-a")
    publish_stories(db, second, [], published_at=NOW + timedelta(hours=1))
    snapshot = get_snapshot(db, "config-a")
    assert snapshot.refresh_id == second
    assert snapshot.stories == ()
    assert get_articles(db, [saved.id]) == (saved,)
