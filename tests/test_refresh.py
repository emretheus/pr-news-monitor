from dataclasses import replace
from unittest.mock import Mock

import pytest

from monitor import pipeline
from monitor.analysis import AnalysisResult, fallback_story
from monitor.config import load_config
from monitor.models import (
    AnalysisStatus,
    Article,
    ArticleGroup,
    RefreshStatus,
    SourceOutcome,
)
from monitor.storage import (
    get_latest_refresh,
    get_snapshot,
    initialize_database,
    publish_stories,
    save_article,
    start_refresh,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "news.db"
    initialize_database(path)
    return path


def group(db, id="a"):
    article = save_article(
        db,
        Article(
            id=id,
            url=f"https://example.com/{id}",
            title="Equinix announces expansion",
            publisher="Example",
            provider="fixture",
        ),
    )
    return ArticleGroup(id, (article,))


def success(group):
    story = replace(
        fallback_story(group, load_config(), "model"),
        analysis_status=AnalysisStatus.SUCCESS,
    )
    return AnalysisResult(story, request_made=True)


def test_published_success_is_reused_without_api_call(monkeypatch, db):
    config = load_config()
    candidate = group(db)
    attempt = start_refresh(db, config.fingerprint)
    publish_stories(db, attempt, [success(candidate).story])
    call = Mock()
    monkeypatch.setattr(pipeline, "analyze_group", call)
    result = pipeline.analyze_groups(
        (candidate,), config, db, api_key="", model="model"
    )
    assert result.reused == 1 and result.requests == 0
    call.assert_not_called()


def test_published_fallback_is_retried(monkeypatch, db):
    config = load_config()
    candidate = group(db)
    attempt = start_refresh(db, config.fingerprint)
    publish_stories(db, attempt, [fallback_story(candidate, config, "model")])
    call = Mock(return_value=success(candidate))
    monkeypatch.setattr(pipeline, "analyze_group", call)
    result = pipeline.analyze_groups(
        (candidate,), config, db, api_key="key", model="model"
    )
    assert result.requests == 1 and result.reused == 0
    assert result.fallbacks == 0


def test_call_limit_and_service_failure_preserve_all_groups(monkeypatch, db):
    a, b = group(db, "a"), group(db, "b")
    call = Mock(return_value=success(a))
    monkeypatch.setattr(pipeline, "analyze_group", call)
    result = pipeline.analyze_groups(
        (a, b), load_config(), db, api_key="key", model="model", max_calls=1
    )
    assert len(result.stories) == 2 and result.fallbacks == 1
    assert call.call_count == 1
    call.reset_mock()
    call.return_value = AnalysisResult(
        fallback_story(a, load_config(), "model"), True, "Service unavailable", True
    )
    result = pipeline.analyze_groups(
        (a, b), load_config(), db, api_key="key", model="model"
    )
    assert result.fallbacks == 2 and call.call_count == 1


def test_full_refresh_publishes_and_reports_partial_analysis(monkeypatch, db):
    candidate = group(db)
    ingestion = pipeline.IngestionResult(
        candidate.articles, (SourceOutcome("newsdata", "success", 1),), 1, 0, 0
    )
    monkeypatch.setattr(pipeline, "ingest_news", lambda *a, **k: ingestion)
    monkeypatch.setattr(
        pipeline,
        "build_recent_groups",
        lambda *a, **k: pipeline.GroupingResult((candidate,), 1, False),
    )
    result = pipeline.refresh_news(
        load_config(), db, newsdata_api_key="key", openrouter_api_key="", model="model"
    )
    assert result.status == RefreshStatus.PARTIAL
    assert result.analysis.fallbacks == 1
    snapshot = get_snapshot(db, load_config().fingerprint)
    assert snapshot.refresh_id == result.refresh_id
    assert snapshot.stories[0].analysis_status == AnalysisStatus.FALLBACK


def test_all_sources_failed_keeps_previous_snapshot_and_skips_llm(monkeypatch, db):
    config = load_config()
    previous = start_refresh(db, config.fingerprint)
    publish_stories(db, previous, [success(group(db)).story])
    ingestion = pipeline.IngestionResult(
        (),
        (SourceOutcome("newsdata", "failed"), SourceOutcome("magazine", "failed")),
        0,
        0,
        0,
    )
    monkeypatch.setattr(pipeline, "ingest_news", lambda *a, **k: ingestion)
    call = Mock()
    monkeypatch.setattr(pipeline, "analyze_groups", call)
    result = pipeline.refresh_news(
        config, db, newsdata_api_key="", openrouter_api_key="", model="model"
    )
    assert result.status == RefreshStatus.FAILED
    assert get_snapshot(db, config.fingerprint).refresh_id == previous
    assert get_latest_refresh(db, config.fingerprint).status == RefreshStatus.FAILED
    call.assert_not_called()


def test_unexpected_analysis_failure_keeps_previous_results_and_records_failed_attempt(
    monkeypatch, db
):
    config = load_config()
    candidate = group(db)
    previous = start_refresh(db, config.fingerprint)
    publish_stories(db, previous, [success(candidate).story])
    ingestion = pipeline.IngestionResult(
        candidate.articles, (SourceOutcome("newsdata", "success", 1),), 0, 0, 1
    )
    monkeypatch.setattr(pipeline, "ingest_news", lambda *a, **k: ingestion)
    monkeypatch.setattr(
        pipeline, "analyze_groups", Mock(side_effect=RuntimeError("Unexpected failure"))
    )
    with pytest.raises(RuntimeError):
        pipeline.refresh_news(
            config, db, newsdata_api_key="key", openrouter_api_key="key", model="model"
        )
    assert get_snapshot(db, config.fingerprint).refresh_id == previous
    assert get_latest_refresh(db, config.fingerprint).status == RefreshStatus.FAILED
