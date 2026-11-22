"""The new provider adapters must be selectable, not island modules."""

from __future__ import annotations

import os

import pytest

from mayaworkbench.config import Settings
from mayaworkbench.models import gateway
from mayaworkbench.models.routing import provider_specs_from_settings


def _settings(provider: str, env: dict[str, str]) -> Settings:
    return Settings.model_validate(
        {
            "model": {
                "provider": provider,
                "model": "test-model",
                "api_key_env": "MAYA_MODEL_API_KEY",
            },
            "data_policy": "BYOK_REMOTE_MODEL",
        }
    )


@pytest.mark.parametrize(
    ("provider", "key_var", "expected"),
    [
        ("anthropic", "MAYA_ANTHROPIC_API_KEY", "anthropic"),
        ("gemini", "MAYA_GEMINI_API_KEY", "gemini"),
    ],
)
def test_provider_factory_and_status_use_provider_specific_keys(
    provider, key_var, expected, monkeypatch
):
    monkeypatch.setenv(key_var, "sk-test-canary")
    settings = _settings(provider, {})
    instance = gateway._default_factory(settings)
    assert instance.name == expected
    assert instance.capabilities().tool_calling is True
    assert gateway.provider_key_env(settings) == key_var
    status = gateway.ModelGateway(
        settings, invocations=None, events=None
    ).status()  # status only reads settings/env
    assert status.key_present is True
    assert status.provider == provider
    spec = provider_specs_from_settings(settings)[0]
    assert spec.mode == expected
    assert spec.key_env == key_var
    assert spec.key_present is True
    # The OpenAI default base URL must not be forwarded to another provider.
    assert "openai.com" not in instance.base_url


def test_explicit_key_env_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "sk-test")
    settings = Settings.model_validate(
        {
            "model": {
                "provider": "gemini",
                "model": "m",
                "api_key_env": "MY_CUSTOM_KEY",
            }
        }
    )
    assert gateway.provider_key_env(settings) == "MY_CUSTOM_KEY"
    assert gateway._default_factory(settings).name == "gemini"
