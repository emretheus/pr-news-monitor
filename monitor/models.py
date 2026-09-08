"""Plain data passed between processing, storage, and the UI."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_date(value: object, timezone_name: str = "UTC") -> datetime | None:
    """Normalize source timestamps; unknown or malformed dates remain unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(timezone_name))
        return result.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def fingerprint(data: object) -> str:
    """Stable hash for JSON-compatible inputs, independent of dictionary order."""
    encoded = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FALLBACK = "fallback"


class AnalysisStatus(StrEnum):
    SUCCESS = "success"
    FALLBACK = "fallback"


class RefreshStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class Article:
    url: str  # Conservatively normalized by discovery, before storage.
    title: str
    publisher: str
    provider: str  # Discovery integration, distinct from the publisher.
    id: str = field(default_factory=lambda: uuid4().hex)
    provider_id: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None
    discovered_at: datetime = field(default_factory=utc_now)
    full_text: str | None = None
    extraction_status: ExtractionStatus = ExtractionStatus.PENDING
    extraction_error: str | None = None

    def __post_init__(self) -> None:
        for value in (self.discovered_at, self.published_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("Article timestamps must include a timezone.")

    @property
    def content_fingerprint(self) -> str:
        return fingerprint(
            {
                "title": self.title,
                "publisher": self.publisher,
                "snippet": self.snippet,
                "full_text": self.full_text,
                "published_at": (
                    self.published_at.astimezone(UTC).isoformat()
                    if self.published_at is not None
                    else None
                ),
            }
        )


@dataclass(frozen=True)
class ArticleGroup:
    """Candidate story before LLM description and relevance analysis."""

    id: str
    articles: tuple[Article, ...]


@dataclass(frozen=True)
class ArticleRelevance:
    article_id: str
    company: bool = False
    competitor: bool = False


@dataclass(frozen=True, kw_only=True)
class Story:
    id: str
    description: str
    articles: tuple[ArticleRelevance, ...]
    analysis_fingerprint: str  # Include context, configuration, model, prompt version.
    analysis_status: AnalysisStatus = AnalysisStatus.SUCCESS

    @property
    def company_relevant(self) -> bool:
        return any(article.company for article in self.articles)

    @property
    def competitor_relevant(self) -> bool:
        return any(article.competitor for article in self.articles)


@dataclass(frozen=True)
class SourceOutcome:
    source: str
    status: Literal["success", "partial", "failed"]
    article_count: int = 0
    message: str | None = None  # Safe user-facing explanation, never raw HTTP errors.


@dataclass(frozen=True, kw_only=True)
class RefreshAttempt:
    id: int
    config_fingerprint: str
    started_at: datetime
    status: RefreshStatus
    finished_at: datetime | None = None
    sources: tuple[SourceOutcome, ...] = ()


@dataclass(frozen=True, kw_only=True)
class StorySnapshot:
    config_fingerprint: str
    refresh_id: int
    published_at: datetime
    stories: tuple[Story, ...]
