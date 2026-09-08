import json
import time
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from monitor import sources
from monitor.config import AppConfig
from monitor.http import FetchError, Page


@pytest.fixture
def config():
    return AppConfig.model_validate(
        {"company": {"name": "Equinix"}, "competitors": [{"name": "Digital Realty"}]}
    )


def news_page(rows, token=None):
    return Page(
        "https://newsdata.io/",
        json.dumps({"status": "success", "results": rows, "nextPage": token}),
        "application/json",
    )


def row(id="1", **changes):
    return {
        "article_id": id,
        "link": f"https://example.com/{id}",
        "title": "News",
        "source_name": "Publisher",
        "content": "ONLY AVAILABLE IN PAID PLANS",
    } | changes


def test_newsdata_pagination_dedup_and_missing_fields(monkeypatch, config):
    fake = Mock(
        side_effect=[
            news_page(
                [
                    row(),
                    row(link="https://example.com/1?utm_source=a"),
                    row("bad", link="file:///bad"),
                ],
                "page-2",
            ),
            news_page([row("2", pubDate="invalid")]),
        ]
    )
    monkeypatch.setattr(sources, "fetch_page", fake)
    result = sources.discover_newsdata(config, "secret", deadline=time.monotonic() + 10)
    assert len(result.articles) == 2
    assert result.articles[0].full_text is None
    assert result.articles[1].published_at is None
    assert parse_qs(urlsplit(fake.call_args_list[1].args[0]).query)["page"] == [
        "page-2"
    ]
    assert result.outcome.status == "partial"


def test_second_page_failure_retains_first_page(monkeypatch, config):
    monkeypatch.setattr(
        sources,
        "fetch_page",
        Mock(side_effect=[news_page([row()], "p2"), FetchError("Rate limited.")]),
    )
    result = sources.discover_newsdata(config, "secret", deadline=time.monotonic() + 10)
    assert len(result.articles) == 1
    assert result.outcome.status == "partial"


def test_repeated_page_token_stops(monkeypatch, config):
    fake = Mock(return_value=news_page([row()], "same"))
    monkeypatch.setattr(sources, "fetch_page", fake)
    result = sources.discover_newsdata(config, "secret", deadline=time.monotonic() + 10)
    assert fake.call_count == 2
    assert "pagination" in result.outcome.message


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"status":"error","results":{"message":"secret"}}']
)
def test_invalid_api_responses_fail_without_exposing_body(monkeypatch, config, body):
    monkeypatch.setattr(
        sources, "fetch_page", lambda *a, **k: Page("", body, "application/json")
    )
    result = sources.discover_newsdata(config, "secret", deadline=time.monotonic() + 10)
    assert result.outcome.status == "failed"
    assert "secret" not in result.outcome.message


def test_queries_split_within_provider_limit():
    extended = AppConfig.model_validate(
        {"company": {"name": "A" * 60}, "competitors": [{"name": "B" * 60}]}
    )
    queries = sources.news_queries(extended)
    assert len(queries) == 2
    assert all(len(query) <= 100 for query in queries)
    assert "A" * 60 in queries[0] and "B" * 60 in queries[1]


def test_newsdata_article_cap_is_enforced(monkeypatch, config):
    config = config.model_copy(
        update={"news": config.news.model_copy(update={"max_articles_per_source": 2})}
    )
    monkeypatch.setattr(
        sources, "fetch_page", lambda *a, **k: news_page([row("1"), row("2"), row("3")])
    )
    result = sources.discover_newsdata(config, "secret", deadline=time.monotonic() + 10)
    assert len(result.articles) == 2
    assert result.outcome.status == "partial"


def xml(urls, index=False):
    root, item = ("sitemapindex", "sitemap") if index else ("urlset", "url")
    return (
        f"<{root}>"
        + "".join(f"<{item}><loc>{url}</loc></{item}>" for url in urls)
        + f"</{root}>"
    )


def test_magazine_month_boundary_filters_non_articles_and_prioritizes_company(
    monkeypatch, config
):
    sept = sources.MAGAZINE + "/sitemap-articles-9-2026.xml"
    aug = sources.MAGAZINE + "/sitemap-articles-8-2026.xml"
    pages = {
        sources.MAGAZINE + "/sitemap.xml": xml([aug, sept], index=True),
        sept: xml(
            [
                sources.MAGAZINE + "/events/event",
                sources.MAGAZINE + "/news/general",
                "https://external.example/news/test",
            ]
        ),
        aug: xml([sources.MAGAZINE + "/news/equinix-acquisition"]),
    }
    monkeypatch.setattr(
        sources, "fetch_page", lambda url, **k: Page(url, pages[url], "application/xml")
    )
    result = sources.discover_magazine(
        config, deadline=time.monotonic() + 10, now=datetime(2026, 9, 2, tzinfo=UTC)
    )
    assert len(result.articles) == 2
    assert result.articles[0].url.endswith("equinix-acquisition")
    assert all(article.published_at is None for article in result.articles)
    assert result.outcome.status == "success"


def test_magazine_rejects_html_disguised_as_xml(monkeypatch, config):
    monkeypatch.setattr(
        sources,
        "fetch_page",
        lambda *a, **k: Page("", "<html>challenge</html>", "text/html"),
    )
    result = sources.discover_magazine(config, deadline=time.monotonic() + 10)
    assert result.outcome.status == "failed"


def test_sitemap_rejects_entities_and_ignores_image_locations():
    with pytest.raises(FetchError):
        sources._sitemap_urls('<!DOCTYPE x [<!ENTITY a "data">]><urlset/>')
    assert sources._sitemap_urls(
        "<urlset><url><loc>article</loc><image><loc>image</loc></image></url></urlset>"
    ) == ["article"]
