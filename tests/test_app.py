import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from monitor import pipeline
from monitor.config import ConfigError, load_config
from monitor.models import (
    AnalysisStatus,
    Article,
    ArticleRelevance,
    RefreshStatus,
    SourceOutcome,
    Story,
)
from monitor.storage import (
    finish_refresh,
    initialize_database,
    publish_stories,
    save_article,
    start_refresh,
)

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "news.db"
    initialize_database(path)
    monkeypatch.setenv("NEWS_MONITOR_DB_PATH", str(path))
    monkeypatch.setenv("NEWS_MONITOR_CONFIG_PATH", str(APP.parent / "config.yaml"))
    monkeypatch.setenv("NEWSDATA_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")
    st.cache_resource.clear()
    yield path
    st.cache_resource.clear()


def seed(db):
    config = load_config()
    article = save_article(
        db,
        Article(
            url="https://example.com/news",
            title="Company's joint expansion",
            publisher="Example News",
            provider="fixture",
            snippet="An expansion announcement.",
        ),
    )
    story = Story(
        id="story",
        description="Shared expansion coverage.",
        articles=(ArticleRelevance(article.id, company=True, competitor=True),),
        analysis_fingerprint="fixture",
        analysis_status=AnalysisStatus.FALLBACK,
    )
    attempt = start_refresh(db, config.fingerprint)
    publish_stories(db, attempt, [story])


def test_initial_load_and_reruns_make_no_refresh_call(db, monkeypatch):
    refresh = Mock()
    monkeypatch.setattr(pipeline, "refresh_news", refresh)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert app.title[0].value == "PR News Monitor"
    assert any("No saved results" in item.value for item in app.info)
    app.run()
    refresh.assert_not_called()


def test_saved_story_appears_in_both_feeds_with_fallback_and_unknown_date(db):
    seed(db)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    company = load_config().company.name
    assert [tab.label for tab in app.tabs] == [
        f"{company} (1)",
        "Competitors (1)",
        "Industry (0)",
    ]
    assert len(app.subheader) == 2
    assert app.subheader[0].value == "Company's joint expansion"
    assert any("Headline fallback" in item.value for item in app.caption)
    assert any("Unknown" in item.value for item in app.caption)
    assert any("Snippet only" in item.value for item in app.caption)


def test_failed_attempt_keeps_saved_feed_visible(db):
    seed(db)
    attempt = start_refresh(db, load_config().fingerprint)
    finish_refresh(
        db,
        attempt,
        status=RefreshStatus.FAILED,
        sources=[SourceOutcome("newsdata", "failed", message="Rate limited.")],
    )
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert len(app.subheader) == 2
    assert any("latest refresh failed" in item.value for item in app.error)


def test_refresh_click_runs_once_and_reenables_button(db, monkeypatch):
    def refresh(*args, **kwargs):
        seed(db)
        return pipeline.RefreshResult(
            1, RefreshStatus.SUCCESS, pipeline.IngestionResult((), (), 0, 0, 0), None
        )

    call = Mock(side_effect=refresh)
    monkeypatch.setattr(pipeline, "refresh_news", call)
    app = AppTest.from_file(APP).run()
    app.button(key="refresh_news").click().run()
    assert not app.exception
    assert call.call_count == 1
    assert not app.button(key="refresh_news").disabled
    app.run()
    assert call.call_count == 1


def test_busy_refresh_disables_button(db, monkeypatch):
    monkeypatch.setattr(pipeline, "refresh_in_progress", lambda: True)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert app.button(key="refresh_news").disabled


def test_changed_configuration_does_not_show_old_classifications(
    db, tmp_path, monkeypatch
):
    seed(db)
    path = tmp_path / "changed.yaml"
    path.write_text(
        "company:\n  name: Another Company\ncompetitors:\n  - name: Another Rival\n"
    )
    monkeypatch.setenv("NEWS_MONITOR_CONFIG_PATH", str(path))
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert not app.subheader
    assert any("new refresh is required" in item.value for item in app.info)
    assert len(app.tabs) == 2  # Old configurations may omit the optional sector.


def test_industry_feed_supports_overlap_and_excludes_unrelated_stories(db, monkeypatch):
    stories = []
    for name, company, competitor, industry in (
        ("Sector regulation", False, False, True),
        ("Shared sector shift", True, True, True),
        ("Unrelated weather", False, False, False),
    ):
        article = save_article(
            db,
            Article(
                url=f"https://example.com/{len(stories)}",
                title=name,
                publisher="Example",
                provider="fixture",
            ),
        )
        stories.append(
            Story(
                id=article.id,
                description=name,
                articles=(ArticleRelevance(article.id, company, competitor, industry),),
                analysis_fingerprint="fixture",
            )
        )
    publish_stories(db, start_refresh(db, load_config().fingerprint), stories)
    refresh = Mock()
    monkeypatch.setattr(pipeline, "refresh_news", refresh)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    company = load_config().company.name
    assert [tab.label for tab in app.tabs] == [
        f"{company} (1)",
        "Competitors (1)",
        "Industry (2)",
    ]
    assert [item.value for item in app.tabs[2].subheader] == [
        "Sector regulation",
        "Shared sector shift",
    ]
    assert any("Relevant to: industry" == item.value for item in app.caption)
    app.run()
    refresh.assert_not_called()


def _load_app_module():
    spec = importlib.util.spec_from_file_location("pr_news_app", str(APP))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_presets_build_valid_configs():
    app_module = _load_app_module()
    base = load_config()
    seen = set()
    for preset in app_module.PRESETS:
        config = app_module.preset_to_config(preset, base)
        assert config.company.name
        assert 1 <= len(config.competitors) <= 10
        assert config.industry is not None
        assert config.news == base.news
        seen.add(config.fingerprint)
    assert len(seen) == len(app_module.PRESETS)


def test_preset_to_config_rejects_unknown_name():
    app_module = _load_app_module()
    with pytest.raises(ConfigError):
        app_module.preset_to_config("No such preset", load_config())


def test_custom_to_config_accepts_names_and_rejects_blanks():
    app_module = _load_app_module()
    base = load_config()
    config = app_module.custom_to_config("Apple", "Samsung, Google", base, "Phones")
    assert config.company.name == "Apple"
    assert [e.name for e in config.competitors] == ["Samsung", "Google"]
    assert config.industry is not None and config.industry.name == "Phones"
    kept = app_module.custom_to_config("Apple", "Samsung", base)
    assert kept.industry == base.industry
    with pytest.raises(ConfigError):
        app_module.custom_to_config("  ", "Samsung", base)
    with pytest.raises(ConfigError):
        app_module.custom_to_config("Apple", "   ", base)
    with pytest.raises(ConfigError):
        app_module.custom_to_config("Apple", "Apple", base)


def test_preset_selection_applies_without_raw_error(db):
    app_module = _load_app_module()
    preset = next(iter(app_module.PRESETS))
    app = AppTest.from_file(APP).run()
    assert not app.exception
    app.segmented_control(key="preset_choice").set_value(preset).run()
    assert not app.exception
    app.button(key="use_preset").click().run()
    assert not app.exception
    assert not any("validation error" in str(item.value) for item in app.error)
    assert any(preset.split(":")[0] in str(item.value) for item in app.success)
