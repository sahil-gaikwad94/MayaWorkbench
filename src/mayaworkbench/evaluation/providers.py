"""Frozen provider/model configurations for reproducible evaluation.

A profile is a *configuration*, never a credential. Keys stay in the
environment; only the provider id, model id and decoding settings are recorded
in scorecards, so two runs with the same profile plus the same harness version
are comparable. No live call is made here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mayaworkbench.errors import UsageError

HARNESS_VERSION = "eval-harness/2.0"


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=60)
    provider: str = Field(min_length=1, max_length=40)
    model_id: str = Field(min_length=1, max_length=160)
    adapter: str = Field(default="openai-compatible", min_length=1, max_length=60)
    temperature: float = 0.0
    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    tool_schema_version: str = "v1"
    harness_version: str = HARNESS_VERSION
    notes: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _id_chars(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("profile id may contain only alphanumeric, '-' and '_'")
        return value

    def public_dict(self) -> dict[str, Any]:
        """A serialisable profile with no possibility of carrying a key."""
        return {
            "id": self.id,
            "provider": self.provider,
            "model_id": self.model_id,
            "adapter": self.adapter,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tool_schema_version": self.tool_schema_version,
            "harness_version": self.harness_version,
            "notes": list(self.notes),
        }


# Two distinct providers/configurations are required by the roadmap. Model ids
# are explicit and frozen; operators may override them with an env var for a
# newer pinned model, which is recorded in the scorecard via the profile id.
DEFAULT_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        id="openai-gpt",
        provider="openai",
        model_id="gpt-4o-mini-2024-07-18",
        adapter="openai-compatible",
        notes=("Pinned OpenAI-compatible configuration; key from MAYA_MODEL_API_KEY.",),
    ),
    ProviderProfile(
        id="anthropic-claude",
        provider="anthropic",
        model_id="claude-3-5-haiku-20241022",
        adapter="anthropic",
        notes=("Pinned Anthropic configuration; key from MAYA_ANTHROPIC_API_KEY.",),
    ),
)


def default_profiles() -> tuple[ProviderProfile, ...]:
    return DEFAULT_PROFILES


def resolve_profile(profile_id: str | None) -> ProviderProfile | None:
    if profile_id is None:
        return None
    for item in DEFAULT_PROFILES:
        if item.id == profile_id:
            return item
    raise UsageError(
        f"unknown provider profile {profile_id!r}",
        hint=f"Available: {', '.join(item.id for item in DEFAULT_PROFILES)}",
    )


def profile_from_env(profile_id: str | None = None) -> ProviderProfile | None:
    """Resolve a profile and allow an explicit pinned model-id override."""
    profile = resolve_profile(profile_id)
    if profile is None:
        return None
    import os

    override = os.environ.get("MAYA_EVAL_MODEL_ID", "").strip()
    if override:
        return profile.model_copy(update={"model_id": override})
    return profile


__all__ = [
    "HARNESS_VERSION",
    "ProviderProfile",
    "default_profiles",
    "resolve_profile",
    "profile_from_env",
]
