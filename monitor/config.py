"""Validate non-secret YAML settings once at application startup."""

import re
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from monitor.models import fingerprint

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigError(ValueError):
    """A configuration problem that can be shown directly to the user."""


class EntityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyText
    aliases: tuple[NonEmptyText, ...] = ()

    @property
    def search_terms(self) -> tuple[str, ...]:
        """Always include the name; collapse case-insensitive duplicate aliases."""
        terms = {term.casefold(): term for term in reversed((self.name, *self.aliases))}
        return tuple(terms[key] for key in sorted(terms))

    def is_mentioned(self, text: str) -> bool:
        """Match whole aliases; this is lexical evidence, not LLM relevance analysis."""
        return any(
            re.search(
                r"(?<!\w)"
                + r"\s+".join(re.escape(word) for word in term.split())
                + r"(?!\w)",
                text,
                re.IGNORECASE,
            )
            for term in self.search_terms
        )


class NewsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Literal["en"] = "en"
    recent_days: int = Field(default=7, strict=True, ge=1, le=30)
    max_articles_per_source: int = Field(default=25, strict=True, ge=1, le=100)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    company: EntityConfig
    competitors: tuple[EntityConfig, ...] = Field(min_length=1, max_length=10)
    industry: EntityConfig | None = None
    news: NewsConfig = Field(default_factory=NewsConfig)

    @model_validator(mode="after")
    def reject_shared_names(self) -> Self:
        owners: dict[str, str] = {}
        for entity in (self.company, *self.competitors):
            for term in entity.search_terms:
                key = term.casefold()
                if key in owners:
                    raise ValueError(
                        f"Name or alias '{term}' is shared by '{owners[key]}' "
                        f"and '{entity.name}'; each entity needs distinct names."
                    )
                owners[key] = entity.name
        return self

    @property
    def fingerprint(self) -> str:
        """Formatting and ordering changes should not invalidate saved analysis."""

        def entity_data(entity: EntityConfig) -> dict:
            return {
                "name": entity.name.casefold(),
                "terms": sorted(term.casefold() for term in entity.search_terms),
            }

        return fingerprint(
            {
                "company": entity_data(self.company),
                "competitors": sorted(
                    (entity_data(entity) for entity in self.competitors),
                    key=lambda entity: entity["name"],
                ),
                "news": self.news.model_dump(),
                **({"industry": entity_data(self.industry)} if self.industry else {}),
            }
        )


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration at {path}.") from exc
    except yaml.YAMLError as exc:
        raise ConfigError("Configuration contains invalid YAML.") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            "Configuration must contain company and competitors mappings."
        )
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"Invalid configuration: {details}") from exc
