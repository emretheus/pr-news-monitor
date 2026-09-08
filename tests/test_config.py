from copy import deepcopy

import pytest
import yaml

from monitor.config import AppConfig, ConfigError, load_config


@pytest.fixture
def settings():
    return {
        "company": {"name": "Equinix", "aliases": ["Equinix"]},
        "competitors": [
            {"name": "Digital Realty", "aliases": ["Digital Realty Trust"]}
        ],
    }


def write_config(tmp_path, settings):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return path


def test_loads_yaml_and_defaults(tmp_path, settings):
    config = load_config(write_config(tmp_path, settings))
    assert config.company.name == "Equinix"
    assert config.competitors[0].search_terms == (
        "Digital Realty",
        "Digital Realty Trust",
    )
    assert config.news.recent_days == 7
    assert config.news.max_articles_per_source == 25


@pytest.mark.parametrize("value", [0, -1, 101, True, "25", 1.5])
def test_rejects_invalid_fetch_limits(tmp_path, settings, value):
    settings["news"] = {"max_articles_per_source": value}
    with pytest.raises(ConfigError, match="news.max_articles_per_source"):
        load_config(write_config(tmp_path, settings))


@pytest.mark.parametrize(
    "entity",
    [
        {"name": "  "},
        {"name": "Competitor", "aliases": [""]},
        {"name": "eQUINIX"},
        {"name": "Competitor", "aliases": [" equinix "]},
    ],
)
def test_rejects_empty_or_conflicting_entity_names(tmp_path, settings, entity):
    settings["competitors"] = [entity]
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, settings))


def test_rejects_duplicate_competitors(tmp_path, settings):
    settings["competitors"] *= 2
    with pytest.raises(ConfigError, match="shared"):
        load_config(write_config(tmp_path, settings))


@pytest.mark.parametrize("contents", ["", "- list", "company: [broken", "null"])
def test_malformed_or_empty_yaml_has_friendly_error(tmp_path, contents):
    path = tmp_path / "config.yaml"
    path.write_text(contents)
    with pytest.raises(ConfigError, match="[Cc]onfiguration"):
        load_config(path)


def test_missing_file_has_friendly_error(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read configuration"):
        load_config(tmp_path / "missing.yaml")


def test_rejects_typo_in_settings(tmp_path, settings):
    settings["news"] = {"recent_day": 7}
    with pytest.raises(ConfigError, match="news.recent_day"):
        load_config(write_config(tmp_path, settings))


def test_fingerprint_ignores_formatting_but_detects_relevance_changes(settings):
    original = AppConfig.model_validate(settings)
    reordered = deepcopy(settings)
    reordered["company"] = {"name": " Equinix ", "aliases": ["equinix", "Equinix"]}
    reordered["competitors"][0]["aliases"] = ["Digital Realty", "digital realty trust"]
    assert AppConfig.model_validate(reordered).fingerprint == original.fingerprint
    reordered["competitors"][0]["aliases"].append("DLR")
    assert AppConfig.model_validate(reordered).fingerprint != original.fingerprint


def test_alias_matching_is_case_insensitive_and_respects_word_boundaries(settings):
    config = AppConfig.model_validate(settings)
    assert config.company.is_mentioned("EQUINIX announced a project.")
    assert not config.company.is_mentioned("Equinixation is not a company alias.")
    assert config.competitors[0].is_mentioned(
        "Digital\nRealty Trust announced a project."
    )


def test_optional_industry_is_validated_and_changes_fingerprint(settings):
    original = AppConfig.model_validate(settings)
    assert original.industry is None
    settings["industry"] = {"name": "Cloud infrastructure", "aliases": ["colocation"]}
    configured = AppConfig.model_validate(settings)
    assert configured.fingerprint != original.fingerprint
    assert configured.industry.is_mentioned("New COLOCATION standards")
    settings["industry"]["aliases"] = ["colocation", "COLocation"]
    assert AppConfig.model_validate(settings).fingerprint == configured.fingerprint
    settings["industry"]["name"] = " "
    with pytest.raises(ValueError):
        AppConfig.model_validate(settings)
