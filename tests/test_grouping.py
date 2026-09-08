from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from monitor.config import load_config
from monitor.grouping import MAX_GROUP_SIZE, group_articles
from monitor.models import Article

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def article(id, title, text="", **changes):
    return Article(
        **(
            {
                "id": id,
                "url": f"https://example.com/{id}",
                "title": title,
                "publisher": f"Publisher {id}",
                "provider": "fixture",
                "snippet": text,
                "published_at": NOW,
                "discovered_at": NOW,
            }
            | changes
        )
    )


def memberships(articles):
    return {
        frozenset(a.id for a in g.articles)
        for g in group_articles(articles, load_config())
    }


def test_same_event_with_different_wording_groups():
    text = "Equinix and CPP Investments completed the acquisition of atNorth for four billion dollars. The Nordic data centre operator serves artificial intelligence customers."
    a = article("a", "Equinix and CPP Investments complete atNorth acquisition", text)
    b = article(
        "b", "CPP Investments and Equinix acquire Nordic operator atNorth", text
    )
    assert memberships([a, b]) == {frozenset({"a", "b"})}


def test_unrelated_events_about_same_company_stay_separate():
    a = article(
        "a",
        "Equinix acquires atNorth in Nordic expansion",
        "Equinix completed the acquisition of atNorth with CPP Investments.",
    )
    b = article(
        "b",
        "Equinix appoints new finance chief",
        "Equinix announced its next chief financial officer following the retirement of its predecessor.",
    )
    assert len(memberships([a, b])) == 2


def test_same_title_different_outlets_preserved_in_one_group():
    a = article("a", "Equinix opens new Berlin campus")
    b = article("b", a.title)
    groups = group_articles([a, b], load_config())
    assert len(groups) == 1
    assert {member.publisher for member in groups[0].articles} == {
        "Publisher a",
        "Publisher b",
    }


def test_full_text_can_supply_evidence_missing_from_snippets():
    a = article("a", "Equinix and CPP Investments complete atNorth acquisition")
    b = article("b", "CPP Investments and Equinix acquire Nordic operator atNorth")
    text = "Nordic operator atNorth acquisition completed by CPP Investments in a four billion dollar transaction supporting artificial intelligence expansion."
    assert len(memberships([a, b])) == 2
    assert (
        len(memberships([replace(a, full_text=text), replace(b, full_text=text)])) == 1
    )


def test_repeated_headline_weeks_apart_does_not_merge():
    a = article("a", "Equinix opens new Berlin campus", "New capacity opens in Berlin.")
    b = article("b", a.title, a.snippet, published_at=NOW + timedelta(days=14))
    assert len(memberships([a, b])) == 2


def test_unknown_dates_require_stronger_evidence():
    a = article(
        "a",
        "Equinix and CPP Investments complete atNorth acquisition",
        "Nordic operator atNorth acquisition completed by CPP Investments.",
    )
    b = article(
        "b", "CPP Investments and Equinix acquire Nordic operator atNorth", a.snippet
    )
    assert len(memberships([a, b])) == 1
    assert len(memberships([a, replace(b, published_at=None)])) == 2


def test_empty_text_and_company_names_alone_do_not_merge():
    assert len(memberships([article("a", ""), article("b", "")])) == 2
    assert len(memberships([article("a", "Equinix"), article("b", "Equinix")])) == 2
    assert group_articles([], load_config()) == ()


def test_boilerplate_body_cannot_merge_unrelated_titles():
    body = "Equinix is a global digital infrastructure business offering data centres and connectivity for enterprise customers around the world."
    a = article("a", "Nordic acquisition closes", body)
    b = article("b", "Finance director resigns", body)
    assert len(memberships([a, b])) == 2


def test_input_order_does_not_change_membership_or_ids():
    articles = [
        article("a", "Equinix opens Berlin campus"),
        article("b", "Equinix opens Berlin campus"),
        article("c", "Digital Realty appoints finance chief"),
    ]
    expected = group_articles(articles, load_config())
    for ordering in permutations(articles):
        assert group_articles(ordering, load_config()) == expected


def test_large_group_splits_without_dropping_articles():
    articles = [
        article(str(i), "Equinix opens Berlin campus")
        for i in range(MAX_GROUP_SIZE + 2)
    ]
    groups = group_articles(articles, load_config())
    assert sorted(len(group.articles) for group in groups) == [2, MAX_GROUP_SIZE]
    assert {a.id for group in groups for a in group.articles} == {
        a.id for a in articles
    }


def test_rejects_duplicate_ids_and_oversized_input():
    a = article("a", "Headline")
    with pytest.raises(ValueError, match="unique"):
        group_articles([a, a], load_config())
    with pytest.raises(ValueError, match="at most"):
        group_articles([a] * 201, load_config())


def test_bridge_article_does_not_chain_unrelated_events():
    first = "Berlin cooling failure outages"
    second = "Singapore expansion construction campus"
    a = article("a", first, first)
    bridge = article(
        "b",
        first + " " + second,
        first + " " + second,
        published_at=NOW + timedelta(hours=1),
    )
    c = article("c", second, second, published_at=NOW + timedelta(hours=2))
    assert len(memberships([a, bridge])) == 1
    assert len(memberships([bridge, c])) == 1
    assert memberships([a, bridge, c]) == {frozenset({"a", "b"}), frozenset({"c"})}


def test_same_announcement_template_for_different_companies_does_not_merge():
    a = article("a", "Equinix raises quarterly dividend")
    b = article("b", "Digital Realty raises quarterly dividend")
    assert len(memberships([a, b])) == 2


def test_overlap_in_article_text_allows_cross_company_coverage():
    body = "Equinix and Digital Realty announce joint network connectivity project connecting Berlin campuses."
    a = article("a", "Equinix announces joint network connectivity project", body)
    b = article(
        "b", "Digital Realty announces joint network connectivity project", body
    )
    assert len(memberships([a, b])) == 1
