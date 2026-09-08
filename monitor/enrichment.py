"""Extract publisher text from already controlled HTTP responses."""

import re
import time
from dataclasses import replace

from bs4 import BeautifulSoup
from newspaper import Article as NewspaperArticle

from monitor.http import FetchError, fetch_page
from monitor.models import Article, ExtractionStatus, parse_date


def enrich_article(article: Article, *, deadline: float) -> Article:
    if article.full_text and article.extraction_status == ExtractionStatus.SUCCESS:
        return article
    result = article
    try:
        page = fetch_page(article.url, deadline=min(deadline, time.monotonic() + 15))
        if page.content_type not in ("text/html", "application/xhtml+xml"):
            raise FetchError("Publisher did not return an HTML article.")
        soup = BeautifulSoup(page.text, "html.parser")

        def metadata(name: str) -> str | None:
            tag = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            value = tag.get("content") if tag else None
            return value.strip() or None if isinstance(value, str) else None

        result = replace(
            article,
            title=article.title or metadata("og:title") or "",
            snippet=article.snippet
            or metadata("og:description")
            or metadata("description"),
            published_at=article.published_at
            or parse_date(metadata("article:published_time")),
        )
        try:
            parsed = NewspaperArticle(
                article.url,
                language="en",
                fetch_images=False,
                follow_meta_refresh=False,
            )
            parsed.download(input_html=page.text, ignore_read_more=True)
            parsed.parse()
        except Exception:  # noqa: BLE001 - isolate failures from the third-party HTML parser.
            # Third-party parsers can raise many exceptions on malformed publisher HTML.
            raise FetchError("Article text could not be extracted.") from None
        title = result.title or parsed.title or ""
        text = parsed.text.strip()
        blocking_titles = (
            "access denied",
            "just a moment",
            "verify you are human",
            "consent required",
            "robot or human",
        )
        if len(text.split()) < 50 or any(
            phrase in title.casefold() for phrase in blocking_titles
        ):
            raise FetchError(
                "Extracted text was too short or appeared to be a blocked page."
            )
        title_terms = set(re.findall(r"[a-z]{5,}", title.casefold()))
        if title_terms and not title_terms.intersection(
            re.findall(r"[a-z]{5,}", text.casefold())
        ):
            raise FetchError("Extracted text did not match the article title.")
        published_at = result.published_at
        if published_at is None and parsed.publish_date is not None:
            published_at = parse_date(parsed.publish_date.isoformat())
        return replace(
            result,
            title=title,
            full_text=text[:100_000],
            published_at=published_at,
            extraction_status=ExtractionStatus.SUCCESS,
            extraction_error=None,
        )
    except FetchError as exc:
        return replace(
            result,
            extraction_status=ExtractionStatus.FALLBACK,
            extraction_error=str(exc),
        )
