"""Discover metadata; neither integration accesses SQLite or the dashboard."""

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

from monitor.config import AppConfig
from monitor.http import FetchError, fetch_page, normalize_url
from monitor.models import Article, SourceOutcome, parse_date, utc_now

MAGAZINE = "https://datacentremagazine.com"


@dataclass(frozen=True)
class DiscoveryResult:
    articles: tuple[Article, ...]
    outcome: SourceOutcome


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def news_queries(config: AppConfig) -> tuple[str, ...]:
    """Split configured terms into the free plan's 100-character query budget."""
    queries = []
    current = ""
    for entity in (config.company, *config.competitors):
        for term in entity.search_terms:
            if '"' in term or "\\" in term or len(term) > 98:
                raise FetchError(
                    "A configured search term cannot fit a NewsData.io query."
                )
            quoted = f'"{term}"'
            candidate = f"{current} OR {quoted}" if current else quoted
            if len(candidate) > 100:
                queries.append(current)
                current = quoted
            else:
                current = candidate
    if current:
        queries.append(current)
    if len(queries) > 10:
        raise FetchError(
            "Too many search terms for the bounded NewsData.io query budget."
        )
    return tuple(queries)


def discover_newsdata(
    config: AppConfig, api_key: str, *, deadline: float
) -> DiscoveryResult:
    articles: dict[str, Article] = {}
    seen_ids: set[str] = set()
    warnings = []
    succeeded = False
    try:
        if not api_key:
            raise FetchError("NewsData.io API key is missing.")
        pending = deque((query, None) for query in news_queries(config))
        seen_pages: set[tuple[str, str]] = set()
        cap = config.news.max_articles_per_source
        page_budget = min(10, max(len(pending), math.ceil(cap / 10)))
        for _ in range(page_budget):
            if not pending or len(articles) >= cap:
                break
            query, token = pending.popleft()
            params = {"apikey": api_key, "q": query, "language": config.news.language}
            if token:
                params["page"] = token
            # No redirects on credential-bearing requests; never log the URL.
            page = fetch_page(
                "https://newsdata.io/api/1/latest?" + urlencode(params),
                deadline=deadline,
                allow_redirects=False,
            )
            try:
                data = json.loads(page.text)
            except ValueError:
                raise FetchError("NewsData.io returned invalid JSON.") from None
            if (
                not isinstance(data, dict)
                or data.get("status") != "success"
                or not isinstance(data.get("results"), list)
            ):
                raise FetchError(
                    "NewsData.io returned an unsuccessful or unexpected response."
                )
            succeeded = True
            for row in data["results"]:
                if len(articles) >= cap:
                    warnings.append("Article limit reached.")
                    break
                if not isinstance(row, dict):
                    warnings.append("Skipped malformed article metadata.")
                    continue
                try:
                    url = normalize_url(row.get("link"))
                except FetchError:
                    warnings.append("Skipped an invalid article URL.")
                    continue
                provider_id = _text(row.get("article_id"))
                if url in articles or (provider_id and provider_id in seen_ids):
                    continue
                articles[url] = Article(
                    url=url,
                    title=_text(row.get("title")) or "",
                    publisher=_text(row.get("source_name"))
                    or _text(row.get("source_id"))
                    or urlsplit(url).hostname,
                    provider="newsdata",
                    provider_id=provider_id,
                    snippet=_text(row.get("description")),
                    published_at=parse_date(
                        row.get("pubDate"), _text(row.get("pubDateTZ")) or "UTC"
                    ),
                )
                if provider_id:
                    seen_ids.add(provider_id)
            next_page = data.get("nextPage")
            if next_page:
                if not isinstance(next_page, str) or (query, next_page) in seen_pages:
                    warnings.append("Stopped repeated or invalid pagination token.")
                else:
                    seen_pages.add((query, next_page))
                    pending.append((query, next_page))
        if pending:
            warnings.append("Retrieval limited by the refresh article/page budget.")
    except FetchError as exc:
        warnings.append(str(exc))
    status = (
        "partial" if warnings and succeeded else "success" if succeeded else "failed"
    )
    return DiscoveryResult(
        tuple(articles.values()),
        SourceOutcome(
            "newsdata",
            status,
            len(articles),
            " ".join(dict.fromkeys(warnings)) or None,
        ),
    )


def _sitemap_urls(text: str) -> list[str]:
    if "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise FetchError("Sitemap contains unsupported XML declarations.")
    try:
        root = ElementTree.fromstring(text)
        if root.tag.rsplit("}", 1)[-1] not in ("sitemapindex", "urlset"):
            raise ValueError
        return [
            node.text.strip()
            for entry in root
            for node in entry
            if node.tag.rsplit("}", 1)[-1] == "loc" and node.text
        ]
    except (ElementTree.ParseError, ValueError):
        raise FetchError("Magazine returned an invalid sitemap.") from None


def discover_magazine(
    config: AppConfig, *, deadline: float, now: datetime | None = None
) -> DiscoveryResult:
    now = now or utc_now()
    candidates: dict[str, Article] = {}
    warnings = []
    succeeded = False
    try:
        index = fetch_page(f"{MAGAZINE}/sitemap.xml", deadline=deadline)
        days = (
            now - timedelta(days=offset)
            for offset in range(config.news.recent_days + 1)
        )
        months = {(day.year, day.month) for day in days}
        maps = []
        for url in _sitemap_urls(index.text):
            match = re.fullmatch(
                r"https://datacentremagazine\.com/sitemap-articles-(\d{1,2})-(\d{4})\.xml",
                url,
            )
            if match and (int(match[2]), int(match[1])) in months:
                maps.append((int(match[2]), int(match[1]), url))
        if not maps:
            raise FetchError("No recent magazine article sitemap was available.")
        for _, _, url in sorted(maps, reverse=True):
            try:
                page = fetch_page(url, deadline=deadline)
                urls = _sitemap_urls(page.text)
                succeeded = True
                for raw in urls:
                    try:
                        candidate = normalize_url(raw)
                    except FetchError:
                        continue
                    parts = urlsplit(candidate)
                    if (
                        parts.netloc != "datacentremagazine.com"
                        or parts.query
                        or not re.fullmatch(r"/(news|articles)/[^/]+", parts.path)
                    ):
                        continue
                    candidates.setdefault(
                        candidate,
                        Article(
                            url=candidate,
                            title="",
                            publisher="Data Centre Magazine",
                            provider="magazine",
                            provider_id=candidate,
                        ),
                    )
            except FetchError as exc:
                warnings.append(str(exc))
    except FetchError as exc:
        warnings.append(str(exc))
    # Prioritize explicit slug matches, but still inspect other articles for indirect mentions.
    terms = [
        term.casefold().replace(" ", "-")
        for entity in (config.company, *config.competitors)
        for term in entity.search_terms
    ]
    ordered = sorted(
        candidates.values(),
        key=lambda article: not any(term in article.url.casefold() for term in terms),
    )
    cap = config.news.max_articles_per_source
    if len(ordered) > cap:
        warnings.append("Magazine candidate limit reached; coverage is sampled.")
    articles = tuple(ordered[:cap])
    status = (
        "partial" if warnings and succeeded else "success" if succeeded else "failed"
    )
    return DiscoveryResult(
        articles,
        SourceOutcome(
            "magazine",
            status,
            len(articles),
            " ".join(dict.fromkeys(warnings)) or None,
        ),
    )
