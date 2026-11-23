"""Configuration and local data-directory resolution.

Design rules enforced here:

* Secrets are never written to the config file. Only the *name* of the
  environment variable that holds a provider key is stored.
* Configuration is a versioned JSON document validated by a strict model.
* Environment variables may override sensitive/operational settings so that a
  user can avoid persisting them at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mayaworkbench.errors import ConfigError

DataPolicy = Literal["LOCAL_ONLY", "BYOK_REMOTE_MODEL"]
ModelProviderKind = Literal[
    "none",
    "openai_compatible",
    "anthropic",
    "gemini",
    "local_openai_compatible",
]

#: Provider-specific key variables used when the user has not overridden
#: ``model.api_key_env``. Values are variable *names*, never credentials.
PROVIDER_KEY_ENV = {
    "anthropic": "MAYA_ANTHROPIC_API_KEY",
    "gemini": "MAYA_GEMINI_API_KEY",
    "local_openai_compatible": "MAYA_LOCAL_MODEL_API_KEY",
}
DEFAULT_MODEL_KEY_ENV = "MAYA_MODEL_API_KEY"

SETTINGS_SCHEMA_VERSION = "1"

DEFAULT_EXCLUDED_DIRS = [
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".maya",
    "dist",
    "build",
    "site-packages",
    ".eggs",
    ".cache",
    "wandb",
    ".ipynb_checkpoints",
]

DEFAULT_SECRET_FILENAME_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".netrc",
    ".pgpass",
    ".aws/credentials",
    "credentials.json",
    "service-account*.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
]

DEFAULT_EXCLUDED_GLOBS = [
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.webp",
    "*.pdf",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.7z",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.safetensors",
    "*.onnx",
    "*.bin",
    "*.parquet",
    "*.npy",
    "*.npz",
    "*.h5",
    "*.hdf5",
    "*.pkl",
    "*.pickle",
    "*.sqlite",
    "*.db",
]


class ModelSettings(BaseModel):
    """Provider-neutral model configuration. No secrets are stored here."""

    model_config = ConfigDict(extra="forbid")

    provider: ModelProviderKind = "none"
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str = DEFAULT_MODEL_KEY_ENV
    timeout_seconds: int = Field(default=120, ge=5, le=900)
    max_output_tokens: int = Field(default=2000, ge=128, le=32000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_input_tokens: int = Field(default=24000, ge=1000, le=400000)
    # Optional user-supplied prices so cost is reported from a stated basis
    # instead of being invented. USD per 1M tokens.
    input_cost_per_mtok: float | None = Field(default=None, ge=0)
    output_cost_per_mtok: float | None = Field(default=None, ge=0)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class RetrievalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_token_budget: int = Field(default=8000, ge=500, le=200000)
    max_hits: int = Field(default=40, ge=1, le=500)
    max_span_lines: int = Field(default=80, ge=5, le=2000)
    span_context_lines: int = Field(default=6, ge=0, le=200)
    max_context_items: int = Field(default=24, ge=1, le=200)
    # Conservative fallback when no provider tokenizer is available.
    chars_per_token: float = Field(default=3.6, gt=0.5, le=8.0)


class IndexingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(default=1_500_000, ge=1024)
    max_total_bytes: int = Field(default=96_000_000, ge=1024)
    max_files: int = Field(default=20000, ge=1)
    excluded_dirs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_DIRS))
    excluded_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_GLOBS))
    extra_included_globs: list[str] = Field(default_factory=list)
    secret_filename_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SECRET_FILENAME_PATTERNS)
    )
    scan_content_for_secrets: bool = True
    store_file_content: bool = True


class ResearchSettings(BaseModel):
    """Limits for document ingestion, claim extraction and code mapping.

    Every bound is explicit because each one is a place where the workbench
    could otherwise silently truncate evidence. Overruns raise or are recorded
    as extraction warnings instead of being dropped without a trace.
    """

    model_config = ConfigDict(extra="forbid")

    max_document_bytes: int = Field(default=25_000_000, ge=1024)
    max_passage_chars: int = Field(default=4000, ge=200, le=200_000)
    min_passage_chars: int = Field(default=24, ge=1, le=10_000)
    max_passages_per_document: int = Field(default=50_000, ge=10)
    max_claims_per_extraction: int = Field(default=200, ge=1, le=20_000)
    extraction_token_budget: int = Field(default=12_000, ge=500, le=200_000)
    max_evidence_spans: int = Field(default=8, ge=1, le=50)
    max_evidence_span_lines: int = Field(default=80, ge=5, le=2000)
    max_candidate_seeds: int = Field(default=6, ge=1, le=50)
    fetch_timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_fetch_bytes: int = Field(default=25_000_000, ge=1024)
    fetch_max_redirects: int = Field(default=4, ge=0, le=10)
    allow_private_network_fetch: bool = False


class ExperimentSettings(BaseModel):
    """Hard local ceilings for the Experiment Lab.

    A definition declares its own budgets, but it cannot exceed these: the
    settings are the outer bound the local machine owner controls. A profile
    that needs more (a full training run) is refused locally instead of being
    silently executed as a smaller run.
    """

    model_config = ConfigDict(extra="forbid")

    max_runtime_seconds: int = Field(default=1800, ge=1, le=86_400)
    max_total_runtime_seconds: int = Field(default=7200, ge=1, le=7 * 86_400)
    max_runs: int = Field(default=20, ge=1, le=100)
    max_artifact_bytes: int = Field(default=20_000_000, ge=1024)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    max_metric_observations: int = Field(default=100_000, ge=1)
    max_metric_file_bytes: int = Field(default=10_000_000, ge=1024)
    max_dataset_fingerprint_bytes: int = Field(default=268_435_456, ge=0)
    default_min_seeds: int = Field(default=2, ge=1, le=100)
    keep_workspaces: bool = False


class Settings(BaseModel):
    """Root settings document persisted at ``$MAYA_HOME/config.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SETTINGS_SCHEMA_VERSION
    data_policy: DataPolicy = "LOCAL_ONLY"
    model: ModelSettings = Field(default_factory=ModelSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    experiments: ExperimentSettings = Field(default_factory=ExperimentSettings)

    def allows_remote_model(self) -> bool:
        return self.data_policy == "BYOK_REMOTE_MODEL"


class AppPaths(BaseModel):
    """Concrete filesystem locations for one local workbench installation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    home: Path
    db_path: Path
    artifacts_dir: Path
    logs_dir: Path
    config_path: Path

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> "AppPaths":
        if home is None:
            home = os.environ.get("MAYA_HOME") or str(Path.home() / ".mayaworkbench")
        root = Path(home).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        return cls(
            home=root,
            db_path=root / "maya.db",
            artifacts_dir=root / "artifacts",
            logs_dir=root / "logs",
            config_path=root / "config.json",
        )

    def ensure_dirs(self) -> None:
        for directory in (self.home, self.artifacts_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # Best effort: keep user data private on POSIX systems.
        if os.name == "posix":
            try:
                self.home.chmod(0o700)
            except OSError:  # pragma: no cover - exotic filesystems
                pass


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load_settings(paths: AppPaths) -> Settings:
    """Load settings from disk then apply environment overrides."""
    payload: dict = {}
    if paths.config_path.exists():
        try:
            payload = json.loads(paths.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Cannot read settings file {paths.config_path}: {exc}",
                hint="Fix or remove the file; `maya init` writes a fresh default config.",
            ) from exc
    try:
        settings = Settings.model_validate(payload or {})
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid settings in {paths.config_path}",
            detail=str(exc),
            hint="Run `maya config show` and compare against `maya config defaults`.",
        ) from exc
    return _apply_env_overrides(settings)


def _apply_env_overrides(settings: Settings) -> Settings:
    updates: dict = {}
    model_updates: dict = {}
    retrieval_updates: dict = {}

    policy = os.environ.get("MAYA_DATA_POLICY")
    if policy:
        if policy not in ("LOCAL_ONLY", "BYOK_REMOTE_MODEL"):
            raise ConfigError(
                "MAYA_DATA_POLICY must be LOCAL_ONLY or BYOK_REMOTE_MODEL",
                hint="See docs/threat-model.md for what each policy permits.",
            )
        updates["data_policy"] = policy

    provider = os.environ.get("MAYA_MODEL_PROVIDER")
    if provider:
        if provider not in ("none", "openai_compatible"):
            raise ConfigError("MAYA_MODEL_PROVIDER must be 'none' or 'openai_compatible'")
        model_updates["provider"] = provider

    for env_name, field in (
        ("MAYA_MODEL_BASE_URL", "base_url"),
        ("MAYA_MODEL_NAME", "model"),
        ("MAYA_MODEL_API_KEY_ENV", "api_key_env"),
    ):
        value = os.environ.get(env_name)
        if value:
            model_updates[field] = value

    max_out = _env_int("MAYA_MODEL_MAX_OUTPUT_TOKENS")
    if max_out is not None:
        model_updates["max_output_tokens"] = max_out
    timeout = _env_float("MAYA_MODEL_TIMEOUT_SECONDS")
    if timeout is not None:
        model_updates["timeout_seconds"] = int(timeout)
    temperature = _env_float("MAYA_MODEL_TEMPERATURE")
    if temperature is not None:
        model_updates["temperature"] = temperature

    budget = _env_int("MAYA_TOKEN_BUDGET")
    if budget is not None:
        retrieval_updates["default_token_budget"] = budget

    if model_updates:
        merged = settings.model.model_dump()
        merged.update(model_updates)
        updates["model"] = merged
    if retrieval_updates:
        merged = settings.retrieval.model_dump()
        merged.update(retrieval_updates)
        updates["retrieval"] = merged

    if not updates:
        # Still round-trip through validation so env-only changes are type-checked.
        return settings
    try:
        return Settings.model_validate({**settings.model_dump(), **updates})
    except ValidationError as exc:
        raise ConfigError(
            "Environment overrides produced invalid settings",
            detail=str(exc),
            hint="Check the MAYA_* environment variables.",
        ) from exc


def save_settings(paths: AppPaths, settings: Settings) -> None:
    """Atomically persist settings (never including secrets)."""
    paths.ensure_dirs()
    payload = settings.model_dump(mode="json")
    tmp = paths.config_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, paths.config_path)
        if os.name == "posix":
            paths.config_path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"Cannot write settings to {paths.config_path}: {exc}") from exc


def provider_key_env(settings: Settings) -> str:
    """Resolve the key variable for the configured provider (never the value).

    A user-set ``model.api_key_env`` always wins. Otherwise the provider's
    conventional variable is used so an Anthropic or Gemini key does not have to
    be re-exported under the OpenAI-compatible name.
    """
    configured = settings.model.api_key_env
    if configured and configured != DEFAULT_MODEL_KEY_ENV:
        return configured
    return PROVIDER_KEY_ENV.get(settings.model.provider, configured or DEFAULT_MODEL_KEY_ENV)


def api_key_from_env(settings: Settings) -> str | None:
    """Read the provider key from its configured environment variable.

    The key is returned only to the provider adapter; it is never logged,
    persisted, or included in exported artifacts. This uses the explicitly
    configured variable; use :func:`provider_key_env` for provider defaults.
    """
    raw = os.environ.get(settings.model.api_key_env)
    return raw or None


def redacted_settings_view(paths: AppPaths, settings: Settings) -> dict:
    key_present = bool(os.environ.get(provider_key_env(settings)))
    return {
        "home": str(paths.home),
        "database": str(paths.db_path),
        "artifacts": str(paths.artifacts_dir),
        "config_file": str(paths.config_path),
        "data_policy": settings.data_policy,
        "model": {
            **settings.model.model_dump(),
            "api_key_env": settings.model.api_key_env,
            "api_key_present": key_present,
        },
        "retrieval": settings.retrieval.model_dump(),
        "indexing": settings.indexing.model_dump(),
        "research": settings.research.model_dump(),
        "experiments": settings.experiments.model_dump(),
    }


__all__ = [
    "AppPaths",
    "Settings",
    "ModelSettings",
    "RetrievalSettings",
    "IndexingSettings",
    "ResearchSettings",
    "ExperimentSettings",
    "DataPolicy",
    "SETTINGS_SCHEMA_VERSION",
    "load_settings",
    "save_settings",
    "api_key_from_env",
    "redacted_settings_view",
]
