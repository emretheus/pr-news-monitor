import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from monitor import enrichment
from monitor.http import FetchError, Page
from monitor.models import Article, ExtractionStatus


@pytest.fixture
def article():
    return Article(
        url="https://example.com/news",
        title="",
        publisher="Publisher",
        provider="fixture",
        snippet="Existing snippet.",
    )


def test_extracts_real_html_without_a_second_download(monkeypatch, article):
    html = (Path(__file__).parent / "fixtures" / "article.html").read_text()
    fetch = Mock(return_value=Page(article.url, html, "text/html"))
    monkeypatch.setattr(enrichment, "fetch_page", fetch)
    result = enrichment.enrich_article(article, deadline=time.monotonic() + 10)
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert len(result.full_text.split()) > 100
    assert result.title == "Equinix Opens New Data Centre in Berlin"
    assert result.published_at == datetime(2026, 9, 8, 10, tzinfo=UTC)
    assert result.snippet == "Existing snippet."
    fetch.assert_called_once()


def test_parser_does_not_follow_embedded_redirect_or_fetch_images(monkeypatch, article):
    import newspaper.network
    import requests

    html = (Path(__file__).parent / "fixtures" / "article.html").read_text()
    html = html.replace(
        "</head>",
        '<meta http-equiv="refresh" content="0; url=http://127.0.0.1/private"></head>',
    )
    html = html.replace(
        "</article>", '<img src="http://127.0.0.1/private.png"></article>'
    )
    monkeypatch.setattr(
        enrichment, "fetch_page", lambda *a, **k: Page(article.url, html, "text/html")
    )
    direct_network = Mock(
        side_effect=AssertionError("Unexpected parser network request")
    )
    monkeypatch.setattr(newspaper.network, "get_html", direct_network)
    monkeypatch.setattr(requests.sessions.Session, "request", direct_network)
    result = enrichment.enrich_article(article, deadline=time.monotonic() + 10)
    assert result.extraction_status == ExtractionStatus.SUCCESS
    direct_network.assert_not_called()


def test_network_failure_preserves_metadata(monkeypatch, article):
    monkeypatch.setattr(
        enrichment,
        "fetch_page",
        Mock(side_effect=FetchError("Source returned HTTP 403.")),
    )
    result = enrichment.enrich_article(article, deadline=time.monotonic() + 10)
    assert result.id == article.id
    assert result.snippet == article.snippet
    assert result.full_text is None
    assert result.extraction_status == ExtractionStatus.FALLBACK
    assert "403" in result.extraction_error


@pytest.mark.parametrize(
    "body,content_type",
    [
        ("<html><title>Access denied</title>blocked</html>", "text/html"),
        ("%PDF-content", "application/pdf"),
    ],
)
def test_unusable_content_falls_back(monkeypatch, article, body, content_type):
    monkeypatch.setattr(
        enrichment, "fetch_page", lambda *a, **k: Page(article.url, body, content_type)
    )
    result = enrichment.enrich_article(article, deadline=time.monotonic() + 10)
    assert result.extraction_status == ExtractionStatus.FALLBACK
    assert result.snippet == article.snippet


def test_parser_exception_is_sanitized_and_page_metadata_survives(monkeypatch, article):
    html = '<meta property="og:title" content="Useful title">'
    monkeypatch.setattr(
        enrichment, "fetch_page", lambda *a, **k: Page(article.url, html, "text/html")
    )
    monkeypatch.setattr(
        enrichment,
        "NewspaperArticle",
        Mock(side_effect=RuntimeError("internal sensitive details")),
    )
    result = enrichment.enrich_article(article, deadline=time.monotonic() + 10)
    assert result.title == "Useful title"
    assert result.extraction_status == ExtractionStatus.FALLBACK
    assert "sensitive" not in result.extraction_error
