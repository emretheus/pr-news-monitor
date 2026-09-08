"""Concrete SQLite operations. Each call owns and closes its connection."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from monitor.models import (
    AnalysisStatus,
    Article,
    ArticleRelevance,
    ExtractionStatus,
    RefreshAttempt,
    RefreshStatus,
    SourceOutcome,
    Story,
    StorySnapshot,
    utc_now,
)

DatabasePath = str | Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_id TEXT,
    snippet TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    full_text TEXT,
    extraction_status TEXT NOT NULL CHECK (extraction_status IN ('pending','success','fallback')),
    extraction_error TEXT,
    content_fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at);
-- One URL can be discovered through several integrations or provider IDs.
CREATE TABLE IF NOT EXISTS article_sources (
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    article_id TEXT NOT NULL REFERENCES articles(id),
    PRIMARY KEY (provider, provider_id)
);
CREATE TABLE IF NOT EXISTS refreshes (
    id INTEGER PRIMARY KEY,
    config_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    sources_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS refreshes_config ON refreshes(config_fingerprint, id);
CREATE TABLE IF NOT EXISTS snapshots (
    config_fingerprint TEXT PRIMARY KEY,
    refresh_id INTEGER NOT NULL REFERENCES refreshes(id),
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
    config_fingerprint TEXT NOT NULL REFERENCES snapshots(config_fingerprint),
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    description TEXT NOT NULL,
    analysis_fingerprint TEXT NOT NULL,
    analysis_status TEXT NOT NULL CHECK (analysis_status IN ('success','fallback')),
    PRIMARY KEY (config_fingerprint, id)
);
CREATE INDEX IF NOT EXISTS stories_analysis ON stories(analysis_fingerprint);
CREATE TABLE IF NOT EXISTS story_articles (
    config_fingerprint TEXT NOT NULL,
    story_id TEXT NOT NULL,
    article_id TEXT NOT NULL REFERENCES articles(id),
    position INTEGER NOT NULL,
    company INTEGER NOT NULL CHECK (company IN (0,1)),
    competitor INTEGER NOT NULL CHECK (competitor IN (0,1)),
    PRIMARY KEY (config_fingerprint, article_id),
    FOREIGN KEY (config_fingerprint, story_id)
        REFERENCES stories(config_fingerprint, id) ON DELETE CASCADE
);
"""


@contextmanager
def _connect(
    path: DatabasePath, *, write: bool = False
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # Reads spanning several tables must see the same committed snapshot.
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with connection:
            yield connection
    finally:
        connection.close()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def initialize_database(path: DatabasePath) -> None:
    """Create a file-backed database; existing records survive repeated startup."""
    if str(path) == ":memory:":
        raise ValueError(
            "Use a file-backed database; each operation opens a connection."
        )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(path, write=True) as connection:
        # executescript commits any pending transaction before running its script.
        connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")


def _article(row: sqlite3.Row) -> Article:
    values = dict(row)
    values.pop("content_fingerprint")
    values["discovered_at"] = datetime.fromisoformat(values["discovered_at"])
    if values["published_at"] is not None:
        values["published_at"] = datetime.fromisoformat(values["published_at"])
    values["extraction_status"] = ExtractionStatus(values["extraction_status"])
    return Article(**values)


def _find_article(
    connection: sqlite3.Connection,
    url: str,
    provider: str,
    provider_id: str | None,
) -> Article | None:
    rows = connection.execute(
        """SELECT * FROM articles WHERE url = ? OR id IN (
            SELECT article_id FROM article_sources WHERE provider = ? AND provider_id = ?
        )""",
        (url, provider, provider_id),
    ).fetchall()
    if len(rows) > 1:
        # Conflicting identities must not silently merge unrelated saved coverage.
        raise ValueError("Provider identity and URL refer to different saved articles.")
    return _article(rows[0]) if rows else None


def find_article(
    path: DatabasePath,
    *,
    url: str,
    provider: str = "",
    provider_id: str | None = None,
) -> Article | None:
    with _connect(path) as connection:
        return _find_article(connection, url, provider, provider_id)


def save_article(path: DatabasePath, incoming: Article) -> Article:
    """Upsert by URL/provider identity; never erase useful text with missing data."""
    incoming = replace(
        incoming,
        title=incoming.title.strip(),
        publisher=incoming.publisher.strip(),
        snippet=(incoming.snippet or "").strip() or None,
        full_text=(incoming.full_text or "").strip() or None,
    )
    with _connect(path, write=True) as connection:
        previous = _find_article(
            connection, incoming.url, incoming.provider, incoming.provider_id
        )
        article = incoming
        if previous is not None:
            keep_extraction = (
                incoming.extraction_status == ExtractionStatus.PENDING
                or (bool(previous.full_text) and not incoming.full_text)
            )
            article = replace(
                incoming,
                id=previous.id,
                provider=previous.provider,
                provider_id=previous.provider_id,
                title=incoming.title.strip() or previous.title,
                publisher=incoming.publisher.strip() or previous.publisher,
                snippet=incoming.snippet or previous.snippet,
                published_at=incoming.published_at or previous.published_at,
                discovered_at=min(incoming.discovered_at, previous.discovered_at),
                full_text=incoming.full_text or previous.full_text,
                extraction_status=(
                    previous.extraction_status
                    if keep_extraction
                    else incoming.extraction_status
                ),
                extraction_error=(
                    previous.extraction_error
                    if keep_extraction
                    else incoming.extraction_error
                ),
            )
        values = asdict(article)
        values["discovered_at"] = _timestamp(article.discovered_at)
        values["published_at"] = (
            _timestamp(article.published_at) if article.published_at else None
        )
        values["content_fingerprint"] = article.content_fingerprint
        connection.execute(
            """INSERT INTO articles VALUES (
                :id, :url, :title, :publisher, :provider, :provider_id, :snippet,
                :published_at, :discovered_at, :full_text, :extraction_status,
                :extraction_error, :content_fingerprint
            ) ON CONFLICT(id) DO UPDATE SET
                url=excluded.url, title=excluded.title, publisher=excluded.publisher,
                snippet=excluded.snippet, published_at=excluded.published_at,
                discovered_at=excluded.discovered_at, full_text=excluded.full_text,
                extraction_status=excluded.extraction_status,
                extraction_error=excluded.extraction_error,
                content_fingerprint=excluded.content_fingerprint""",
            values,
        )
        if incoming.provider_id:
            connection.execute(
                "INSERT OR IGNORE INTO article_sources VALUES (?, ?, ?)",
                (incoming.provider, incoming.provider_id, article.id),
            )
        return article


def get_articles(path: DatabasePath, article_ids: Sequence[str]) -> tuple[Article, ...]:
    """Read requested articles in caller order; missing IDs are omitted."""
    if not article_ids:
        return ()
    with _connect(path) as connection:
        placeholders = ",".join("?" for _ in article_ids)
        rows = connection.execute(
            f"SELECT * FROM articles WHERE id IN ({placeholders})",
            tuple(article_ids),
        ).fetchall()
        by_id = {row["id"]: _article(row) for row in rows}
        return tuple(
            by_id[article_id] for article_id in article_ids if article_id in by_id
        )


def list_recent_articles(
    path: DatabasePath,
    *,
    since: datetime,
    limit: int = 200,
) -> tuple[Article, ...]:
    if not 1 <= limit <= 1000:
        raise ValueError("Article read limit must be between 1 and 1000.")
    with _connect(path) as connection:
        rows = connection.execute(
            """SELECT * FROM articles WHERE COALESCE(published_at, discovered_at) >= ?
               ORDER BY published_at IS NULL, published_at DESC, discovered_at DESC, id
               LIMIT ?""",
            (_timestamp(since), limit),
        ).fetchall()
        return tuple(_article(row) for row in rows)


def start_refresh(
    path: DatabasePath,
    config_fingerprint: str,
    *,
    started_at: datetime | None = None,
) -> int:
    with _connect(path, write=True) as connection:
        cursor = connection.execute(
            "INSERT INTO refreshes(config_fingerprint, started_at, status) VALUES (?, ?, ?)",
            (
                config_fingerprint,
                _timestamp(started_at or utc_now()),
                RefreshStatus.RUNNING,
            ),
        )
        return cursor.lastrowid


def _finish_refresh(
    connection: sqlite3.Connection,
    refresh_id: int,
    status: RefreshStatus,
    sources: Sequence[SourceOutcome],
    finished_at: datetime,
) -> None:
    if status not in (
        RefreshStatus.SUCCESS,
        RefreshStatus.PARTIAL,
        RefreshStatus.FAILED,
    ):
        raise ValueError("A finished refresh needs a terminal status.")
    cursor = connection.execute(
        """UPDATE refreshes SET status = ?, finished_at = ?, sources_json = ?
           WHERE id = ? AND status = 'running'""",
        (
            status,
            _timestamp(finished_at),
            json.dumps([asdict(source) for source in sources]),
            refresh_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("Refresh does not exist or has already finished.")


def finish_refresh(
    path: DatabasePath,
    refresh_id: int,
    *,
    status: RefreshStatus,
    sources: Sequence[SourceOutcome] = (),
    finished_at: datetime | None = None,
) -> None:
    """Record an attempt without replacing previously published stories."""
    with _connect(path, write=True) as connection:
        _finish_refresh(
            connection, refresh_id, status, sources, finished_at or utc_now()
        )


def publish_stories(
    path: DatabasePath,
    refresh_id: int,
    stories: Sequence[Story],
    *,
    status: RefreshStatus = RefreshStatus.SUCCESS,
    sources: Sequence[SourceOutcome] = (),
    published_at: datetime | None = None,
) -> None:
    """Atomically replace one configuration's stories and finish its refresh."""
    if status not in (RefreshStatus.SUCCESS, RefreshStatus.PARTIAL):
        raise ValueError(
            "Only successful or partially successful refreshes can publish."
        )
    now = published_at or utc_now()
    with _connect(path, write=True) as connection:
        refresh = connection.execute(
            "SELECT * FROM refreshes WHERE id = ?", (refresh_id,)
        ).fetchone()
        if refresh is None or refresh["status"] != RefreshStatus.RUNNING:
            raise ValueError("Refresh does not exist or has already finished.")
        config = refresh["config_fingerprint"]
        current = connection.execute(
            "SELECT refresh_id FROM snapshots WHERE config_fingerprint = ?",
            (config,),
        ).fetchone()
        if current is not None and current["refresh_id"] > refresh_id:
            raise ValueError("A newer refresh has already published results.")
        connection.execute(
            """INSERT INTO snapshots VALUES (?, ?, ?) ON CONFLICT(config_fingerprint)
               DO UPDATE SET refresh_id=excluded.refresh_id, published_at=excluded.published_at""",
            (config, refresh_id, _timestamp(now)),
        )
        connection.execute(
            "DELETE FROM stories WHERE config_fingerprint = ?", (config,)
        )
        for position, story in enumerate(stories):
            if not story.articles or not story.description.strip():
                raise ValueError(
                    "Each story needs articles and a nonempty description."
                )
            connection.execute(
                "INSERT INTO stories VALUES (?, ?, ?, ?, ?, ?)",
                (
                    config,
                    story.id,
                    position,
                    story.description,
                    story.analysis_fingerprint,
                    story.analysis_status,
                ),
            )
            connection.executemany(
                "INSERT INTO story_articles VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        config,
                        story.id,
                        article.article_id,
                        index,
                        article.company,
                        article.competitor,
                    )
                    for index, article in enumerate(story.articles)
                ],
            )
        _finish_refresh(connection, refresh_id, status, sources, now)


def get_snapshot(path: DatabasePath, config_fingerprint: str) -> StorySnapshot | None:
    with _connect(path) as connection:
        snapshot = connection.execute(
            "SELECT * FROM snapshots WHERE config_fingerprint = ?",
            (config_fingerprint,),
        ).fetchone()
        if snapshot is None:
            return None
        rows = connection.execute(
            "SELECT * FROM stories WHERE config_fingerprint = ? ORDER BY position",
            (config_fingerprint,),
        ).fetchall()
        memberships = connection.execute(
            "SELECT * FROM story_articles WHERE config_fingerprint = ? ORDER BY position",
            (config_fingerprint,),
        ).fetchall()
        by_story: dict[str, list[ArticleRelevance]] = {row["id"]: [] for row in rows}
        for member in memberships:
            by_story[member["story_id"]].append(
                ArticleRelevance(
                    member["article_id"],
                    bool(member["company"]),
                    bool(member["competitor"]),
                )
            )
        stories = tuple(
            Story(
                id=row["id"],
                description=row["description"],
                articles=tuple(by_story[row["id"]]),
                analysis_fingerprint=row["analysis_fingerprint"],
                analysis_status=AnalysisStatus(row["analysis_status"]),
            )
            for row in rows
        )
        return StorySnapshot(
            config_fingerprint=config_fingerprint,
            refresh_id=snapshot["refresh_id"],
            published_at=datetime.fromisoformat(snapshot["published_at"]),
            stories=stories,
        )


def get_latest_refresh(
    path: DatabasePath, config_fingerprint: str
) -> RefreshAttempt | None:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM refreshes WHERE config_fingerprint = ? ORDER BY id DESC LIMIT 1",
            (config_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        return RefreshAttempt(
            id=row["id"],
            config_fingerprint=config_fingerprint,
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"])
            if row["finished_at"]
            else None,
            status=RefreshStatus(row["status"]),
            sources=tuple(
                SourceOutcome(**source) for source in json.loads(row["sources_json"])
            ),
        )
