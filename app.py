"""Streamlit presentation layer. Processing and SQL live in monitor/."""

import html
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from monitor.config import AppConfig, ConfigError, load_config
from monitor.http import FetchError, normalize_url
from monitor.models import (
    AnalysisStatus,
    Article,
    ExtractionStatus,
    RefreshStatus,
    Story,
)
from monitor.pipeline import RefreshInProgress, refresh_in_progress, refresh_news
from monitor.storage import (
    get_articles,
    get_latest_refresh,
    get_snapshot,
    initialize_database,
)

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RuntimeSettings:
    config: AppConfig
    database: Path
    newsdata_key: str = field(repr=False)
    openrouter_key: str = field(repr=False)
    model: str


def setting(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value.strip()
    try:
        return str(st.secrets.get(name, default)).strip()
    except FileNotFoundError:
        return default


@st.cache_resource(show_spinner=False)
def startup() -> RuntimeSettings:
    """Resolve settings once; YAML/environment edits require restarting the app."""
    load_dotenv(ROOT / ".env", override=False)
    config = load_config(setting("NEWS_MONITOR_CONFIG_PATH", str(ROOT / "config.yaml")))
    database = Path(
        setting("NEWS_MONITOR_DB_PATH", str(ROOT / "data" / "news.db"))
    ).expanduser()
    if not database.is_absolute():
        database = ROOT / database
    initialize_database(database)
    return RuntimeSettings(
        config,
        database,
        setting("NEWSDATA_API_KEY"),
        setting("OPENROUTER_API_KEY"),
        setting("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
    )


PRESETS: dict[str, dict[str, object]] = {
    "Data centres: Equinix vs Digital Realty": {
        "company": ("Equinix", ["Equinix"]),
        "competitors": [("Digital Realty", ["Digital Realty", "Digital Realty Trust"])],
        "industry": (
            "Data centres",
            ["data centre", "data center", "data centers", "datacenter", "datacenters", "colocation"],
        ),
    },
    "Cloud: AWS vs Azure + Google Cloud": {
        "company": ("Amazon Web Services", ["Amazon AWS"]),
        "competitors": [("Microsoft Azure", ["Azure"]), ("Google Cloud", ["Google Cloud Platform"])],
        "industry": (
            "Cloud infrastructure",
            ["cloud computing", "cloud infrastructure", "cloud services", "public cloud"],
        ),
    },
    "Phones: Apple vs Samsung": {
        "company": ("Apple", ["Apple Inc", "iPhone", "MacBook"]),
        "competitors": [("Samsung Electronics", ["Samsung", "Samsung Galaxy"])],
        "industry": (
            "Consumer electronics",
            ["smartphone", "smartphones", "consumer electronics", "wearable devices"],
        ),
    },
    "Drinks: Coca-Cola vs PepsiCo": {
        "company": ("Coca-Cola", ["The Coca-Cola Company", "Coca Cola"]),
        "competitors": [("PepsiCo", ["Pepsi"])],
        "industry": (
            "Beverage industry",
            ["beverage industry", "soft drinks", "bottled drinks", "beverage market"],
        ),
    },
    "EVs: Tesla vs BYD + Rivian": {
        "company": ("Tesla", ["Tesla Motors"]),
        "competitors": [("BYD", ["BYD Auto"]), ("Rivian", ["Rivian Automotive"])],
        "industry": (
            "Electric vehicles",
            ["electric vehicle", "electric vehicles", "battery electric", "EV charging"],
        ),
    },
}


def preset_to_config(preset: str, base: AppConfig) -> AppConfig:
    """Build via plain dicts so construction never depends on module identity."""
    try:
        spec = PRESETS[preset]
    except KeyError:
        raise ConfigError("Unknown preset.") from None
    company_name, company_aliases = spec["company"]  # type: ignore[misc]
    competitors = spec["competitors"]  # type: ignore[assignment]
    industry_name, industry_aliases = spec["industry"]  # type: ignore[misc]
    try:
        return AppConfig.model_validate(
            {
                "company": {"name": company_name, "aliases": list(company_aliases)},
                "competitors": [
                    {"name": name, "aliases": list(aliases)}
                    for name, aliases in competitors  # type: ignore[misc]
                ],
                "industry": {"name": industry_name, "aliases": list(industry_aliases)},
                "news": base.news.model_dump(),
            }
        )
    except ValueError as exc:
        raise ConfigError("This preset is currently invalid.") from exc


def custom_to_config(
    company: str, competitors: str, base: AppConfig, industry: str = ""
) -> AppConfig:
    names = [part.strip() for part in competitors.split(",") if part.strip()]
    if not company.strip():
        raise ConfigError("Company name must not be blank.")
    if not names:
        raise ConfigError("Add at least one competitor, comma separated.")
    try:
        return AppConfig.model_validate(
            {
                "company": {"name": company.strip(), "aliases": [company.strip()]},
                "competitors": [{"name": name, "aliases": [name]} for name in names],
                "industry": (
                    {"name": industry.strip(), "aliases": [industry.strip()]}
                    if industry.strip()
                    else (base.industry.model_dump() if base.industry else None)
                ),
                "news": base.news.model_dump(),
            }
        )
    except ValueError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc


def effective_config(base: AppConfig) -> AppConfig:
    custom = st.session_state.get("custom_config")
    return custom if isinstance(custom, AppConfig) else base


def safe_text(text: str) -> str:
    """Render source/model text literally, including Markdown links and images."""
    return re.sub(
        r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", html.escape(text, quote=False)
    )


def display_date(value: datetime | None) -> str:
    return (
        value.astimezone(UTC).strftime("%d %b %Y · %H:%M UTC") if value else "Unknown"
    )


def render_article(
    article: Article,
    *,
    company: bool = False,
    competitor: bool = False,
    industry: bool = False,
) -> None:
    st.markdown(f"**{safe_text(article.title or 'Untitled article')}**")
    st.caption(
        safe_text(
            f"{article.publisher or 'Unknown source'} · Published {display_date(article.published_at)}"
        )
    )
    relevance = [
        name
        for name, relevant in (
            ("company", company),
            ("competitors", competitor),
            ("industry", industry),
        )
        if relevant
    ]
    if relevance:
        st.caption("Relevant to: " + " + ".join(relevance))
    st.markdown(safe_text(article.snippet or "No article description available."))
    if article.extraction_status == ExtractionStatus.SUCCESS:
        st.caption("Full text available for analysis")
    else:
        st.caption("Snippet only · Full text unavailable or not yet retrieved")
    try:
        st.link_button("Read original article", normalize_url(article.url))
    except FetchError:
        st.caption("Original article link unavailable")


def render_story(story: Story, articles: dict[str, Article]) -> None:
    labels = {label.article_id: label for label in story.articles}
    members = [
        articles[label.article_id]
        for label in story.articles
        if label.article_id in articles
    ]
    with st.container(border=True):
        title = members[0].title if members and members[0].title else "Untitled story"
        st.subheader(safe_text(title), divider=False)
        dates = [article.published_at for article in members if article.published_at]
        publishers = len({article.publisher for article in members})
        st.caption(
            f"{len(members)} article(s) · {publishers} source(s) · Latest publication {display_date(max(dates) if dates else None)}"
        )
        if story.analysis_status == AnalysisStatus.FALLBACK:
            st.caption("Headline fallback · Relevance based on keyword matching")
        else:
            st.caption("AI summary · Based on available article text")
        st.markdown(safe_text(story.description))
        if any(
            article.extraction_status != ExtractionStatus.SUCCESS for article in members
        ):
            st.caption("Some coverage was analyzed using snippets only.")
        with st.expander(f"View original coverage ({len(members)})"):
            for index, article in enumerate(members):
                if index:
                    st.divider()
                label = labels.get(article.id)
                render_article(
                    article,
                    company=bool(label and label.company),
                    competitor=bool(label and label.competitor),
                    industry=bool(label and label.industry),
                )


def _preset_card(preset: str, base: AppConfig) -> None:
    """Preview card for one demo preset with a single apply action."""
    spec = PRESETS[preset]
    company_name, _ = spec["company"]  # type: ignore[misc]
    competitors = [name for name, _ in spec["competitors"]]  # type: ignore[misc]
    industry_name, _ = spec["industry"]  # type: ignore[misc]
    with st.container(border=True):
        st.markdown(f"**{safe_text(preset)}**")
        st.caption(
            safe_text(
                f"{company_name} vs {', '.join(competitors)} · Sector: {industry_name}"
            )
        )
        if st.button("Use this preset", key="use_preset", type="primary"):
            try:
                st.session_state["custom_config"] = preset_to_config(preset, base)
                st.session_state["custom_label"] = preset
                st.rerun()
            except ConfigError as exc:
                st.error(str(exc))


def render_config_selector(base: AppConfig) -> AppConfig:
    """Demo presets + custom names. YAML stays the fallback; selection needs refresh."""
    active = effective_config(base)
    if st.session_state.get("custom_config") is not None:
        st.success(
            safe_text(
                f"Active: {st.session_state.get('custom_label') or 'Custom selection'} — "
                "press Refresh news to fetch coverage for it."
            ),
            icon="✅",
        )
    with st.expander("Tracked entities & demo presets", expanded=False):
        st.caption("One click switches demo companies. The YAML file stays the fallback.")
        choice = st.segmented_control(
            "Try an example",
            options=list(PRESETS.keys()),
            key="preset_choice",
        )
        if choice is not None:
            _preset_card(choice, base)
        else:
            st.caption("Pick a pill above to preview an example.")
        st.divider()
        with st.form("custom_entities"):
            st.markdown("**Or track your own names**")
            company = st.text_input(
                "Company", value="", placeholder="e.g. Apple", key="company_input"
            )
            competitors = st.text_input(
                "Competitors (comma separated)",
                value="",
                placeholder="e.g. Samsung, Google",
                key="competitors_input",
            )
            industry = st.text_input(
                "Sector (optional, blank keeps the YAML sector)",
                value="",
                placeholder="e.g. Consumer electronics",
                key="industry_input",
            )
            submitted = st.form_submit_button("Apply custom", type="primary")
            if submitted:
                try:
                    st.session_state["custom_config"] = custom_to_config(
                        company, competitors, base, industry
                    )
                    st.session_state["custom_label"] = "Custom selection"
                    st.rerun()
                except ConfigError as exc:
                    st.error(str(exc))
        if st.button("Reset to YAML", key="reset_config"):
            st.session_state["custom_config"] = None
            st.session_state["custom_label"] = None
            st.rerun()
    return active


def render_overview(
    stories: tuple[Story, ...], articles: dict[str, Article]
) -> None:
    """Small PR-useful visual: volume metrics + top publishers. No show-only charts."""
    if not stories:
        return
    company = sum(1 for s in stories if s.company_relevant)
    competitor = sum(1 for s in stories if s.competitor_relevant)
    fallbacks = sum(1 for s in stories if s.analysis_status != AnalysisStatus.SUCCESS)
    with st.expander("Coverage overview", expanded=False):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Stories", len(stories))
        m2.metric("Company", company)
        m3.metric("Competitors", competitor)
        m4.metric("Fallbacks", fallbacks)
        counts = Counter(
            (articles[l.article_id].publisher or "Unknown")
            for s in stories
            for l in s.articles
            if l.article_id in articles
        )
        if counts:
            st.caption("Articles per publisher (top 10)")
            top = dict(counts.most_common(10))
            try:
                import pandas as pd

                st.bar_chart(pd.Series(top))
            except Exception:
                st.bar_chart(top)


def request_refresh() -> None:
    st.session_state["refresh_requested"] = True


def run_refresh(settings: RuntimeSettings, config: AppConfig, placeholder) -> None:
    # Consume the request before doing work: a later rerun must not repeat API calls.
    st.session_state["refresh_requested"] = False
    try:
        with (
            placeholder.container(),
            st.status("Refreshing news…", expanded=True) as status,
        ):
            result = refresh_news(
                config,
                settings.database,
                newsdata_api_key=settings.newsdata_key,
                openrouter_api_key=settings.openrouter_key,
                model=settings.model,
                progress=lambda message: status.update(label=message),
            )
            messages = list(result.analysis.warnings) if result.analysis else []
            if result.grouping_limited:
                messages.append("Only the latest 200 saved articles were grouped.")
            st.session_state["refresh_messages"] = (
                config.fingerprint,
                messages,
            )
            status.update(label="Refresh complete", state="complete", expanded=False)
    except RefreshInProgress:
        st.session_state["refresh_messages"] = (
            config.fingerprint,
            [
                "Another visitor is refreshing the news. Wait, then reload the page to see their results."
            ],
        )
    except Exception:  # noqa: BLE001 - show a safe message, not credentials or an internal traceback.
        st.session_state["refresh_messages"] = (
            config.fingerprint,
            [
                "The refresh could not finish. Previously published results remain available. Check the local setup before trying again."
            ],
        )
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="PR News Monitor", page_icon="📰", layout="wide")
    try:
        settings = startup()
    except ConfigError as error:
        st.error(str(error))
        st.stop()
    except (OSError, sqlite3.Error):
        st.error(
            "News storage could not be opened. Check that the configured data directory is writable."
        )
        st.stop()
    config = effective_config(settings.config)
    render_config_selector(settings.config)
    st.caption("PRESS COVERAGE · COMPANY & COMPETITORS & INDUSTRY")
    heading, controls = st.columns([4, 1], vertical_alignment="center")
    with heading:
        st.title("PR News Monitor")
        st.badge(config.company.name, color="blue")
        for entity in config.competitors:
            st.badge(entity.name, color="orange")
        if config.industry:
            st.badge(config.industry.name, color="violet")
        if st.session_state.get("custom_config") is not None:
            st.badge(
                str(st.session_state.get("custom_label") or "Custom"), color="green"
            )
        else:
            st.badge("YAML default", color="gray")
    requested = st.session_state.get("refresh_requested", False)
    busy = refresh_in_progress()
    with controls:
        st.button(
            "Refresh news",
            key="refresh_news",
            type="primary",
            on_click=request_refresh,
            disabled=busy or requested,
            width="stretch",
        )
    progress_area = st.empty()
    if busy:
        st.info(
            "A refresh is running. Saved coverage is shown below; reload after it finishes."
        )
    if not settings.newsdata_key:
        st.warning(
            "NewsData.io is not configured. Refresh can still retrieve Data Centre Magazine coverage."
        )
    if not settings.openrouter_key or not settings.model:
        st.warning(
            "AI analysis is not configured. New stories will use headline and keyword fallbacks."
        )
    try:
        snapshot = get_snapshot(settings.database, config.fingerprint)
        latest = get_latest_refresh(settings.database, config.fingerprint)
        ids = (
            [label.article_id for story in snapshot.stories for label in story.articles]
            if snapshot
            else []
        )
        articles = {
            article.id: article for article in get_articles(settings.database, ids)
        }
    except sqlite3.Error:
        st.error(
            "Saved news could not be read. Check the local database before refreshing."
        )
        st.stop()
    if snapshot:
        st.caption(
            f"Last published update: {display_date(snapshot.published_at)} · Recent window: {config.news.recent_days} days"
        )
    else:
        st.info(
            "No saved results for these monitoring settings. Select Refresh news to load coverage. If settings changed, a new refresh is required."
        )
    if latest:
        if latest.status == RefreshStatus.FAILED:
            st.error(
                "The latest refresh failed. Showing the last published results, if available."
            )
        elif latest.status == RefreshStatus.PARTIAL:
            st.warning(
                "The latest refresh has limited coverage or fallback analysis. See source status and story labels."
            )
        elif latest.status == RefreshStatus.RUNNING and not busy:
            st.warning(
                "The previous refresh was interrupted. Select Refresh news to try again."
            )
        with st.expander(
            "Source status",
            expanded=latest.status in (RefreshStatus.FAILED, RefreshStatus.PARTIAL),
        ):
            st.caption(f"Last attempt: {display_date(latest.started_at)}")
            names = {"newsdata": "NewsData.io", "magazine": "Data Centre Magazine"}
            for source in latest.sources:
                st.markdown(
                    safe_text(
                        f"{names.get(source.source, source.source)} · {source.status} · {source.article_count} articles"
                    )
                )
                if source.message:
                    st.caption(safe_text(source.message))
    message_config, messages = st.session_state.get("refresh_messages", (None, []))
    if message_config == config.fingerprint:
        for message in messages:
            st.warning(safe_text(message))
    stories = snapshot.stories if snapshot else ()
    render_overview(stories, articles)
    company = [story for story in stories if story.company_relevant]
    competitor = [story for story in stories if story.competitor_relevant]
    feeds = [company, competitor]
    tab_names = [
        f"{safe_text(config.company.name)} ({len(company)})",
        f"Competitors ({len(competitor)})",
    ]
    if config.industry:
        industry = [story for story in stories if story.industry_relevant]
        feeds.append(industry)
        tab_names.append(f"Industry ({len(industry)})")
    for index, (tab, feed) in enumerate(zip(st.tabs(tab_names), feeds, strict=True)):
        with tab:
            if index == 2:
                st.caption(
                    safe_text(
                        f"Sector: {config.industry.name}. Broader industry developments may also appear in the company feeds."
                    )
                )
            if not feed and snapshot:
                st.info(
                    "No matching stories in the saved coverage. Refresh later for new articles."
                )
            for story in feed:
                render_story(story, articles)
    st.caption(
        "Coverage is a bounded sample. NewsData.io's free feed is delayed; original publication dates may be unavailable."
    )
    if requested:
        run_refresh(settings, config, progress_area)


if __name__ == "__main__":
    main()
