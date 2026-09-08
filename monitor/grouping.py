"""Conservative lexical story grouping. No network, database, or LLM calls."""

import re
from collections.abc import Sequence
from datetime import timedelta

import numpy as np
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

from monitor.config import AppConfig
from monitor.models import Article, ArticleGroup, fingerprint

MAX_ARTICLES = 200
MAX_GROUP_SIZE = 8
MAX_DATE_GAP = timedelta(hours=72)
SIMILARITY_THRESHOLD = 0.42
UNKNOWN_DATE_THRESHOLD = 0.65
TITLE_WEIGHT = 0.65
BODY_WORD_LIMIT = 600


def _similarities(texts: list[str], stop_words: list[str]) -> np.ndarray:
    vectorizer = TfidfVectorizer(
        stop_words=stop_words,
        ngram_range=(1, 2),
        sublinear_tf=True,
        strip_accents="unicode",
        max_features=20_000,
    )
    # An empty title/body corpus is legitimate; it provides no evidence to merge.
    analyze = vectorizer.build_analyzer()
    if not any(analyze(text) for text in texts):
        return np.zeros((len(texts), len(texts)))
    matrix = vectorizer.fit_transform(texts)
    return (matrix @ matrix.T).toarray()  # TF-IDF rows are already L2-normalized.


def group_articles(
    articles: Sequence[Article], config: AppConfig
) -> tuple[ArticleGroup, ...]:
    """Assign each article once, comparing only to a group's fixed representative.

    Prefer a missed merge over silently combining different events. Rebuilding with
    new articles can change TF-IDF weights and memberships; IDs follow membership.
    """
    if len(articles) > MAX_ARTICLES:
        raise ValueError(f"Grouping accepts at most {MAX_ARTICLES} recent articles.")
    if len({article.id for article in articles}) != len(articles):
        raise ValueError("Grouping requires unique article IDs.")
    if not articles:
        return ()
    ordered = sorted(
        articles,
        key=lambda article: (
            article.published_at is None,
            article.published_at or article.discovered_at,
            article.url,
            article.id,
        ),
    )
    # Shared company names alone are not evidence of a shared event.
    entity_words = {
        word
        for entity in (config.company, *config.competitors)
        for term in entity.search_terms
        for word in re.findall(r"\b\w\w+\b", term.lower())
    }
    stop_words = sorted(set(ENGLISH_STOP_WORDS) | entity_words)
    titles = _similarities([article.title for article in ordered], stop_words)
    body_texts = [
        " ".join((article.full_text or article.snippet or "").split()[:BODY_WORD_LIMIT])
        for article in ordered
    ]
    bodies = _similarities(body_texts, stop_words)
    mentioned = [
        {
            entity.name
            for entity in (config.company, *config.competitors)
            if entity.is_mentioned(article.title + " " + body)
        }
        for article, body in zip(ordered, body_texts, strict=True)
    ]
    scores = TITLE_WEIGHT * titles + (1 - TITLE_WEIGHT) * bodies

    memberships: list[list[int]] = []
    for index, article in enumerate(ordered):
        best_group = None
        best_score = -1.0
        for group in memberships:
            if len(group) >= MAX_GROUP_SIZE:
                continue
            representative_index = group[0]
            representative = ordered[representative_index]
            if (
                mentioned[index]
                and mentioned[representative_index]
                and mentioned[index].isdisjoint(mentioned[representative_index])
            ):
                continue
            dated = (
                article.published_at is not None
                and representative.published_at is not None
            )
            left_date = article.published_at or article.discovered_at
            right_date = representative.published_at or representative.discovered_at
            if abs(left_date - right_date) > MAX_DATE_GAP:
                continue
            threshold = SIMILARITY_THRESHOLD if dated else UNKNOWN_DATE_THRESHOLD
            score = float(scores[index, representative_index])
            # Shared boilerplate in article bodies must not merge unrelated headlines.
            if titles[index, representative_index] < 0.2:
                continue
            if score + 1e-9 >= threshold and score > best_score:
                best_group = group
                best_score = score
        if best_group is None:
            memberships.append([index])
        else:
            best_group.append(index)

    groups = [
        ArticleGroup(
            id=fingerprint(sorted(ordered[index].id for index in group)),
            articles=tuple(ordered[index] for index in group),
        )
        for group in memberships
    ]
    # Dashboard order is independent of the oldest-first representative selection.
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                not any(article.published_at for article in group.articles),
                -max(
                    article.published_at.timestamp()
                    for article in group.articles
                    if article.published_at
                )
                if any(article.published_at for article in group.articles)
                else 0,
                group.id,
            ),
        )
    )
