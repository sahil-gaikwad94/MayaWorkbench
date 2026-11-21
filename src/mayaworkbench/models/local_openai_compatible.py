"""Clearly-labelled local OpenAI-compatible model adapter (WP7).

The existing :class:`~mayaworkbench.models.openai_compatible.OpenAICompatibleProvider`
already speaks the widely-used ``/chat/completions`` shape. This module adds a
*mode*, not a new protocol: ``LOCAL_OPENAI_COMPATIBLE`` is for Ollama,
llama.cpp, vLLM and similar endpoints the user runs themselves.

Security rules:

* a local endpoint (loopback / localhost) is allowed under ``LOCAL_ONLY``;
* a remote host is refused unless the data policy explicitly permits remote
  models (``BYOK_REMOTE_MODEL``);
* URLs with embedded credentials are always refused;
* the key is read from the environment, used only for the outgoing request and
  never emitted by :meth:`LocalOpenAICompatibleProvider.describe` or events.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from typing import Any

from mayaworkbench.config import Settings
from mayaworkbench.errors import PolicyError
from mayaworkbench.models.openai_compatible import OpenAICompatibleProvider

#: Mode literal owned by WP7 (the config enum in ``config.py`` predates it).
LOCAL_OPENAI_COMPATIBLE = "LOCAL_OPENAI_COMPATIBLE"

#: Placeholder sent to local servers that do not require authentication. It is
#: not a credential and is never reported as one.
_UNAUTHENTICATED_SENTINEL = "local-unauthenticated"

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_local_host(base_url: str) -> bool:
    """True only for loopback/localhost HTTP(S) endpoints."""
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().strip("[]")
    if host in _LOCAL_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_unspecified)


class LocalOpenAICompatibleProvider(OpenAICompatibleProvider):
    """OpenAI-compatible adapter that enforces the local-mode data policy."""

    name = "local_openai_compatible"
    mode = LOCAL_OPENAI_COMPATIBLE

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        data_policy: str = "LOCAL_ONLY",
        key_env: str = "MAYA_LOCAL_MODEL_API_KEY",
        timeout_seconds: int = 120,
        max_output_tokens: int = 2000,
        temperature: float = 0.0,
        max_retries: int = 2,
        input_cost_per_mtok: float | None = None,
        output_cost_per_mtok: float | None = None,
    ):
        parsed = urllib.parse.urlsplit((base_url or "").strip())
        if parsed.username or parsed.password:
            raise PolicyError(
                "Local model URLs with embedded credentials are refused",
                hint="Put any key in the environment named by key_env.",
            )
        local = is_local_host(base_url)
        if not local and data_policy != "BYOK_REMOTE_MODEL":
            raise PolicyError(
                f"{LOCAL_OPENAI_COMPATIBLE} refuses remote host {parsed.hostname!r} "
                "while the data policy is LOCAL_ONLY",
                hint=(
                    "Point base_url at a local server, or explicitly set "
                    "MAYA_DATA_POLICY=BYOK_REMOTE_MODEL if you accept sending data off-host."
                ),
            )
        supplied = api_key if api_key is not None else os.environ.get(key_env)
        self._data_policy = data_policy
        self._key_env = key_env
        self._using_sentinel = not bool(supplied)
        super().__init__(
            base_url=base_url,
            model=model,
            api_key=supplied or _UNAUTHENTICATED_SENTINEL,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            max_retries=max_retries,
            input_cost_per_mtok=input_cost_per_mtok,
            output_cost_per_mtok=output_cost_per_mtok,
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret description safe for CLI output and events."""
        return {
            "name": self.name,
            "mode": self.mode,
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "local_host": is_local_host(self.base_url),
            "data_policy": self._data_policy,
            "key_env": self._key_env,
            "key_present": not self._using_sentinel,
            "structured_output": self.capabilities().structured_output,
            "streaming": self.capabilities().streaming,
            "note": "Key value is never included in output, results or events.",
        }


def build_local_provider(settings: Settings) -> LocalOpenAICompatibleProvider:
    """Create the adapter from settings plus WP7's local-model environment keys.

    Only names are read from configuration. Keys come from the environment and
    are passed straight to the adapter, never into a result payload.
    """
    configured = settings.model.base_url
    base_url = os.environ.get("MAYA_LOCAL_MODEL_BASE_URL") or configured
    if not is_local_host(base_url) and not settings.allows_remote_model():
        # Fail closed with a usable local default instead of silently targeting
        # a configured remote endpoint under LOCAL_ONLY.
        base_url = "http://127.0.0.1:11434/v1"
    model = os.environ.get("MAYA_LOCAL_MODEL_NAME") or settings.model.model or "local-model"
    return LocalOpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=os.environ.get("MAYA_LOCAL_MODEL_API_KEY"),
        data_policy=settings.data_policy,
        key_env="MAYA_LOCAL_MODEL_API_KEY",
        timeout_seconds=settings.model.timeout_seconds,
        max_output_tokens=settings.model.max_output_tokens,
        temperature=settings.model.temperature,
        max_retries=settings.model.max_retries,
        input_cost_per_mtok=settings.model.input_cost_per_mtok,
        output_cost_per_mtok=settings.model.output_cost_per_mtok,
    )


__all__ = [
    "LOCAL_OPENAI_COMPATIBLE",
    "LocalOpenAICompatibleProvider",
    "is_local_host",
    "build_local_provider",
]
