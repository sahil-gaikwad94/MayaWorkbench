"""WP7 model platform tests — all providers, HTTP and keys are fakes.

No test in this module opens a socket, contacts a real provider, or uses a real
API key. The only network-capable adapter is exercised through an injected HTTP
function or through pure URL validation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mayaworkbench.cli.model_commands import register_model_commands
from mayaworkbench.cli.output import Console
from mayaworkbench.config import Settings
from mayaworkbench.errors import ModelError, PolicyError
from mayaworkbench.models.anthropic_adapter import AnthropicProvider
from mayaworkbench.models.base import (
    ChatMessage,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)
from mayaworkbench.models.gemini_adapter import GeminiProvider
from mayaworkbench.models.capabilities import (
    ChatModel,
    ConnectorError,
    ConnectorRegistry,
    ConnectorResult,
    EmbeddingModel,
    HashingEmbeddingModel,
    LexicalOverlapReranker,
    Reranker,
    ToolConnector,
    UnsupportedVisionModel,
    VisionModel,
    VisionRequest,
    cosine,
    supports_streaming,
)
from mayaworkbench.models.connectors import build_default_connector_registry
from mayaworkbench.models.connectors.github import GitHubReadOnlyConnector, HttpReply
from mayaworkbench.models.local_openai_compatible import (
    LocalOpenAICompatibleProvider,
    build_local_provider,
    is_local_host,
)
from mayaworkbench.models.openai_compatible import OpenAICompatibleProvider
from mayaworkbench.models.routing import (
    ModelRouter,
    ProviderHealthTracker,
    ProviderSpec,
    RouteRequest,
    RoutingError,
    explain_route,
    provider_specs_from_settings,
)


# --------------------------------------------------------------------------- #
# Helpers / fakes                                                              #
# --------------------------------------------------------------------------- #
def _spec(
    name: str,
    *,
    tier: str = "reliable",
    local: bool = True,
    priority: int = 100,
    model: str = "model-1",
    structured: bool = True,
    max_context_tokens: int | None = None,
    cost_per_mtok: float | None = None,
) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        model=model,
        mode="LOCAL_OPENAI_COMPATIBLE" if local else "openai_compatible",
        local=local,
        priority=priority,
        tier=tier,  # type: ignore[arg-type]
        capabilities=ModelCapabilities(structured_output=structured),
        max_context_tokens=max_context_tokens,
        cost_per_mtok=cost_per_mtok,
        key_env="MAYA_FAKE_KEY",
    )


def _response(provider: str) -> ModelResponse:
    return ModelResponse(
        text="ok",
        usage=ModelUsage(total_tokens=1),
        provider=provider,
        model="model-1",
        latency_ms=1,
    )


class _CallableChatModel:
    name = "fake_chat"
    model = "fake-chat-1"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(structured_output=True, streaming=False)

    def generate(self, request: ModelRequest) -> ModelResponse:
        return _response(self.name)


class _FakeConnector:
    name = "fake"

    def __init__(self) -> None:
        self.revoked = False
        self.state: dict[str, Any] = {"cached": "value"}

    def operations(self) -> tuple[str, ...]:
        return ("read_only_op",)

    def supports(self, operation: str) -> bool:
        return operation in self.operations()

    def invoke(self, operation: str, params: dict[str, Any]) -> ConnectorResult:
        self.state["last"] = params
        return ConnectorResult(
            connector=self.name,
            operation=operation,
            target="fake/local",
            data={"ok": True},
            request_hash="a" * 64,
            response_hash="b" * 64,
            retrieved_at="2026-01-01T00:00:00.000000+00:00",
        )

    def revoke(self) -> None:
        self.state.clear()
        self.revoked = True

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "writes_supported": False}


def _github_http(*, body: Any = None, status: int = 200, final_url: str | None = None):
    """Return an injected HTTP function plus a capture list (never a socket)."""
    payload = json.dumps(body if body is not None else [{"number": 1}]).encode("utf-8")
    calls: list[dict[str, Any]] = []

    def http(method: str, url: str, headers: dict[str, str]) -> HttpReply:
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        return HttpReply(status=status, body=payload, final_url=final_url)

    return http, calls


def _cli(args: list[str], tmp_path: Path, monkeypatch):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_model_commands(sub)
    parsed = parser.parse_args(args)
    stream = io.StringIO()
    parsed.console = Console(json_mode=True, stream=stream)
    parsed.home = str(tmp_path / "cli-home")
    monkeypatch.delenv("MAYA_GITHUB_TOKEN", raising=False)
    code = parsed.func(parsed)
    return code, json.loads(stream.getvalue()), parsed


# --------------------------------------------------------------------------- #
# 1. Capability interfaces                                                    #
# --------------------------------------------------------------------------- #
def test_chat_model_extends_provider_contract() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9/v1", model="local-1", api_key="not-a-real-key"
    )
    assert isinstance(provider, ChatModel)
    assert provider.capabilities().structured_output is True
    # SSE is genuinely consumed (buffered in generate(stream=True)), so the
    # truthful contract is "streaming supported" — not the old blanket False.
    assert supports_streaming(provider) is True
    assert provider.capabilities().streaming is True
    fake = _CallableChatModel()
    assert isinstance(fake, ChatModel)
    assert fake.generate(ModelRequest(purpose="test", messages=[])).provider == "fake_chat"


def test_embedding_reranker_and_vision_interfaces_fail_closed() -> None:
    embedder = HashingEmbeddingModel()
    reranker = LexicalOverlapReranker()
    vision = UnsupportedVisionModel()
    assert isinstance(embedder, EmbeddingModel)
    assert isinstance(reranker, Reranker)
    assert isinstance(vision, VisionModel)
    capabilities = vision.capabilities()
    assert capabilities.supported is False
    assert not any((capabilities.screenshots, capabilities.diagrams, capabilities.charts))
    with pytest.raises(ModelError) as raised:
        vision.inspect(VisionRequest(image_ref="x.png", question="what is this?"))
    assert raised.value.error_class == "unsupported"


def test_registry_is_policy_authority_not_the_model() -> None:
    connector = _FakeConnector()
    registry = ConnectorRegistry(
        policy_gate=lambda name, op, params: (_ for _ in ()).throw(ConnectorError("gate refused"))
        if params.get("dangerous")
        else None
    )
    registry.register(connector)
    with pytest.raises(ConnectorError):
        registry.invoke("fake", "create_issue", {})  # not in the allowlist
    with pytest.raises(ConnectorError, match="gate refused"):
        registry.invoke("fake", "read_only_op", {"dangerous": True})
    result = registry.invoke("fake", "read_only_op", {"owner": "acme"})
    assert result.policy_decision == "ALLOWED_BY_REGISTRY_ALLOWLIST"
    assert result.operation == "read_only_op"
    with pytest.raises(ConnectorError):
        registry.invoke("missing", "read_only_op", {})
    registry.revoke("fake")
    assert connector.revoked is True
    assert connector.state == {}
    assert registry.names() == ()


# --------------------------------------------------------------------------- #
# 2. Routing policy                                                           #
# --------------------------------------------------------------------------- #
def test_task_type_context_and_uncertainty_change_the_route() -> None:
    specs = [
        _spec("fast", tier="fast", priority=10, max_context_tokens=8192),
        _spec("long", tier="long_context", priority=20, max_context_tokens=64000),
        _spec("remote", tier="reasoning", local=False, priority=5, max_context_tokens=32000),
    ]
    router = ModelRouter(specs, data_policy="BYOK_REMOTE_MODEL", token_budget=50000)
    request = RouteRequest(
        task_type="paper_reasoning",
        context_tokens=40000,
        uncertain=True,
        token_budget=50000,
        data_policy="BYOK_REMOTE_MODEL",
    )
    decision = router.route(request)
    assert decision.provider == "long"
    assert "pdf_extraction" in decision.deterministic_companions
    assert "uncertain=true" in " ".join(decision.reasons)
    assert "context 40000 exceeds model limit 8192" in decision.rejected["fast"]
    assert decision.policy_authority == "workbench_policy"
    assert decision.model_is_authority is False


def test_local_only_policy_rejects_remote_providers() -> None:
    router = ModelRouter(
        [_spec("remote", local=False, priority=1), _spec("local", local=True, priority=9)],
        data_policy="LOCAL_ONLY",
    )
    decision = router.route(RouteRequest(task_type="general", data_policy="LOCAL_ONLY"))
    assert decision.provider == "local"
    assert "data_policy LOCAL_ONLY" in decision.rejected["remote"]
    remote_only = ModelRouter([_spec("remote", local=False)], data_policy="LOCAL_ONLY")
    with pytest.raises(RoutingError) as raised:
        remote_only.route(RouteRequest(data_policy="LOCAL_ONLY"))
    assert raised.value.rejected["remote"]


def test_context_over_budget_and_structured_requirement_fail_closed() -> None:
    router = ModelRouter([_spec("local")], token_budget=1000)
    over = RouteRequest(context_tokens=2000, token_budget=1000)
    explanation = explain_route(over, router)
    assert explanation["eligible"] is False
    assert "exceeds budget" in explanation["reason"]
    with pytest.raises(RoutingError):
        router.route(over)
    no_structured = ModelRouter([_spec("local", structured=False)])
    explanation = explain_route(
        RouteRequest(require_structured_output=True), no_structured
    )
    assert explanation["eligible"] is False
    assert "structured output" in explanation["rejected"]["local"]


def test_explain_route_is_deterministic_and_key_free(monkeypatch) -> None:
    monkeypatch.setenv("MAYA_FAKE_KEY", "sk-this-value-must-never-appear")
    router = ModelRouter([_spec("local")], data_policy="LOCAL_ONLY")
    request = RouteRequest(task_type="code_review", uncertain=True, context_tokens=512)
    first = explain_route(request, router)
    second = explain_route(request, router)
    assert first == second
    rendered = json.dumps(first)
    assert "sk-this-value-must-never-appear" not in rendered
    assert first["policy_authority"] == "workbench_policy"
    assert first["model_can_authorise_side_effects"] is False
    assert "secret_scan" in first["decision"]["deterministic_companions"]


# --------------------------------------------------------------------------- #
# 3. Provider health and fallback                                             #
# --------------------------------------------------------------------------- #
def test_failure_falls_back_to_secondary_and_changes_later_routes() -> None:
    router = ModelRouter(
        [
            _spec("primary", tier="fast", priority=10),
            _spec("secondary", tier="reasoning", priority=20),
        ],
        data_policy="LOCAL_ONLY",
    )
    request = RouteRequest(task_type="general")

    def call(spec: ProviderSpec) -> ModelResponse:
        if spec.name == "primary":
            raise ModelError("provider offline", error_class="server", retryable=True)
        return _response(spec.name)

    outcome = router.invoke(request, call)
    assert outcome.decision.provider == "secondary"
    assert outcome.decision.fallback_used is True
    assert outcome.attempted == ["primary", "secondary"]
    assert outcome.response.provider == "secondary"
    health = router.health.snapshot()
    assert health["providers"]["primary"]["failures"] == 1
    assert health["providers"]["secondary"]["successes"] == 1

    router.invoke(request, call)  # second consecutive primary failure
    assert router.route(request).provider == "secondary"  # routed around when unhealthy
    assert "provider health" in router.evaluate(request).rejected["primary"]


def test_all_providers_fail_with_redacted_failure_history() -> None:
    router = ModelRouter([_spec("primary")], data_policy="LOCAL_ONLY")

    def call(spec: ProviderSpec) -> ModelResponse:
        raise ModelError("rejected sk-secrettoken123456", error_class="auth")

    with pytest.raises(RoutingError) as raised:
        router.invoke(RouteRequest(), call)
    assert raised.value.failures[0]["error_class"] == "auth"
    assert "sk-secrettoken123456" not in json.dumps(raised.value.failures)
    assert "[REDACTED]" in raised.value.failures[0]["message"]


def test_health_history_persists_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "health.json"
    tracker = ProviderHealthTracker(path=path)
    tracker.record_success("primary", latency_ms=12)
    tracker.record_failure("primary", "server", latency_ms=9)
    reloaded = ProviderHealthTracker(path=path)
    snapshot = reloaded.snapshot("primary")["providers"]["primary"]
    assert snapshot["observations"] == 2
    assert snapshot["successes"] == 1
    assert snapshot["failures"] == 1
    assert snapshot["last_error_class"] == "server"
    assert "sk-" not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 4. Deterministic embeddings and reranking                                   #
# --------------------------------------------------------------------------- #
def test_hashing_embedding_is_deterministic_and_provider_free() -> None:
    first = HashingEmbeddingModel(dimensions=128)
    second = HashingEmbeddingModel(dimensions=128)
    text = "causal attention mask"
    assert first.embed_query(text) == second.embed_query(text)
    assert first.embed_documents([text, "other"])[0] == first.embed_query(text)
    vector = first.embed_query(text)
    assert len(vector) == 128
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-6
    related = cosine(vector, first.embed_query("attention mask padding"))
    unrelated = cosine(vector, first.embed_query("quantum banana telescope"))
    assert related > unrelated
    assert first.describe()["semantic"] is False


def test_lexical_reranker_is_stable_and_bounded() -> None:
    reranker = LexicalOverlapReranker()
    candidates = ["unrelated text", "causal attention mask", "attention mask padding"]
    ranked = reranker.rank("attention mask", candidates)
    assert [item.index for item in ranked[:2]] == [1, 2]
    assert ranked[0].score >= ranked[1].score >= ranked[2].score
    assert ranked == reranker.rank("attention mask", candidates)
    assert len(reranker.rank("attention mask", candidates, top_k=1)) == 1
    empty = reranker.rank("", candidates)
    assert [item.index for item in empty] == [0, 1, 2]
    assert all(item.score == 0.0 for item in empty)


# --------------------------------------------------------------------------- #
# 5. Local OpenAI-compatible mode                                             #
# --------------------------------------------------------------------------- #
def test_local_mode_accepts_loopback_and_refuses_remote_by_default() -> None:
    local = LocalOpenAICompatibleProvider(
        base_url="http://127.0.0.1:11434/v1", model="llama", api_key=None, data_policy="LOCAL_ONLY"
    )
    assert local.describe()["local_host"] is True
    assert local.describe()["key_present"] is False
    assert is_local_host("http://localhost:8000/v1") is True
    assert is_local_host("https://api.openai.com/v1") is False
    with pytest.raises(PolicyError, match="refuses remote host"):
        LocalOpenAICompatibleProvider(
            base_url="https://api.openai.com/v1", model="gpt", api_key="fake", data_policy="LOCAL_ONLY"
        )
    allowed = LocalOpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        model="gpt",
        api_key="fake",
        data_policy="BYOK_REMOTE_MODEL",
    )
    assert allowed.describe()["data_policy"] == "BYOK_REMOTE_MODEL"
    with pytest.raises(PolicyError, match="embedded credentials"):
        LocalOpenAICompatibleProvider(
            base_url="https://user:pass@127.0.0.1:11434/v1", model="llama"
        )


def test_local_adapter_never_emits_key_values(monkeypatch) -> None:
    monkeypatch.setenv("MAYA_LOCAL_MODEL_API_KEY", "sk-local-secret-999999")
    settings = Settings()
    adapter = build_local_provider(settings)
    payload = json.dumps(adapter.describe())
    assert "sk-local-secret-999999" not in payload
    assert adapter.describe()["key_present"] is True
    assert adapter.describe()["key_env"] == "MAYA_LOCAL_MODEL_API_KEY"


# --------------------------------------------------------------------------- #
# 6. GitHub read-only connector                                               #
# --------------------------------------------------------------------------- #
def test_github_injected_http_hashes_and_provenance() -> None:
    body = [{"number": 7, "title": "Fix the thing"}]
    http, calls = _github_http(body=body)
    connector = GitHubReadOnlyConnector(token="ghp_fake_token", http=http)
    result = connector.invoke("list_issues", {"owner": "acme", "repo": "widgets", "state": "open"})
    assert result.data == body
    assert result.read_only is True
    assert result.connector == "github"
    assert result.target == "api.github.com/repos/acme/widgets/issues"
    url = calls[0]["url"]
    assert result.request_hash == hashlib.sha256(
        json.dumps({"method": "GET", "url": url}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.response_hash == hashlib.sha256(json.dumps(body).encode()).hexdigest()
    dumped = json.dumps(result.model_dump(mode="json"))
    assert "ghp_fake_token" not in dumped
    assert calls[0]["headers"]["Authorization"] == "Bearer ghp_fake_token"


def test_github_supports_exactly_three_read_operations() -> None:
    http, calls = _github_http(body={"number": 7})
    connector = GitHubReadOnlyConnector(token="fake", http=http)
    assert connector.operations() == ("list_issues", "get_issue", "list_pr_checks")
    assert connector.describe()["writes_supported"] is False
    issue = connector.invoke("get_issue", {"owner": "acme", "repo": "widgets", "number": 7})
    assert issue.target == "api.github.com/repos/acme/widgets/issues/7"
    checks = connector.invoke(
        "list_pr_checks", {"owner": "acme", "repo": "widgets", "ref": "abc123"}
    )
    assert checks.target.endswith("/commits/abc123/check-runs")
    with pytest.raises(ConnectorError, match="read-only"):
        connector.invoke("create_issue", {"owner": "acme"})
    with pytest.raises(ConnectorError, match="Missing required"):
        connector.invoke("get_issue", {"owner": "acme"})
    with pytest.raises(ConnectorError, match="state"):
        connector.invoke(
            "list_issues", {"owner": "acme", "repo": "widgets", "state": "delete"}
        )
    with pytest.raises(ConnectorError, match="Invalid owner"):
        connector.invoke("list_issues", {"owner": "../etc", "repo": "widgets"})
    assert all(call["method"] == "GET" for call in calls)


def test_github_allowlist_and_ssrf_refusal_run_even_with_injected_http() -> None:
    connector = GitHubReadOnlyConnector(token="fake", http=_github_http()[0])
    with pytest.raises(PolicyError, match="non-allowlisted host"):
        connector._validate_url("https://api.github.com.evil.test/repos/a/b/issues")
    with pytest.raises(PolicyError, match="only https"):
        connector._validate_url("http://api.github.com/repos/a/b/issues")
    with pytest.raises(PolicyError, match="embedded credentials"):
        connector._validate_url("https://user:pass@api.github.com/repos/a/b/issues")
    with pytest.raises(PolicyError, match="non-allowlisted host"):
        local_allowed = GitHubReadOnlyConnector(
            token="fake",
            http=_github_http()[0],
            allowed_hosts={"localhost"},
            api_base="https://localhost",
        )
        local_allowed._validate_url("https://localhost/repos/a/b/issues")
    redirecting = GitHubReadOnlyConnector(
        token="fake",
        http=_github_http(final_url="https://127.0.0.1/admin")[0],
    )
    with pytest.raises(PolicyError, match="non-allowlisted host"):
        redirecting.invoke("list_issues", {"owner": "a", "repo": "b"})


def test_github_token_from_environment_and_revoke(monkeypatch) -> None:
    monkeypatch.setenv("MAYA_GITHUB_TOKEN", "ghp_env_fake_token")
    connector = GitHubReadOnlyConnector(http=_github_http()[0])
    assert connector.describe()["token_present"] is True
    connector.invoke("list_issues", {"owner": "a", "repo": "b"})
    assert connector._state
    connector.revoke()
    assert connector.is_revoked() is True
    assert connector.describe()["token_present"] is False
    assert connector._state == {}
    with pytest.raises(ConnectorError, match="revoked"):
        connector.invoke("list_issues", {"owner": "a", "repo": "b"})
    with pytest.raises(PolicyError, match="No GitHub token"):
        GitHubReadOnlyConnector(http=_github_http()[0], token_env="MAYA_UNSET_GITHUB_TOKEN").invoke(
            "list_issues", {"owner": "a", "repo": "b"}
        )


def test_connectors_read_only_and_registry_revoke() -> None:
    registry = build_default_connector_registry(http=_github_http()[0], token="fake")
    assert registry.names() == ("github",)
    assert registry.describe()[0]["mode"] == "READ_ONLY"
    assert "create_issue" not in registry.describe()[0]["operations"]
    registry.revoke("github")
    assert registry.names() == ()
    with pytest.raises(ConnectorError):
        registry.invoke("github", "list_issues", {"owner": "a", "repo": "b"})


# --------------------------------------------------------------------------- #
# 7. CLI commands                                                             #
# --------------------------------------------------------------------------- #
def test_cli_model_and_connector_commands_json(tmp_path: Path, monkeypatch) -> None:
    code, payload, _ = _cli(["model", "list"], tmp_path, monkeypatch)
    assert code == 0
    assert payload["key_values_emitted"] is False
    assert payload["interfaces"]["vision_model"]["capabilities"]["supported"] is False
    assert payload["local_adapter"]["mode"] == "LOCAL_OPENAI_COMPATIBLE"

    code, payload, _ = _cli(
        [
            "model", "route", "explain",
            "--task-type", "paper_reasoning",
            "--context-tokens", "2000",
            "--uncertain",
            "--data-policy", "LOCAL_ONLY",
        ],
        tmp_path,
        monkeypatch,
    )
    assert code == 0
    assert payload["eligible"] is False  # default config has no local model name
    assert payload["model_can_authorise_side_effects"] is False

    code, payload, _ = _cli(["model", "health"], tmp_path, monkeypatch)
    assert code == 0
    assert "primary" in payload["providers"]

    code, payload, _ = _cli(["connector", "list"], tmp_path, monkeypatch)
    assert code == 0
    assert payload["connectors"][0]["operations"] == [
        "list_issues",
        "get_issue",
        "list_pr_checks",
    ]

    code, payload, _ = _cli(["connector", "test", "github"], tmp_path, monkeypatch)
    assert code == 0
    assert payload["network_call"] is False
    assert payload["status"] in {"CONFIG_OK", "NOT_CONFIGURED"}

    code, payload, _ = _cli(["connector", "revoke", "github"], tmp_path, monkeypatch)
    assert code == 0
    assert payload["status"] == "REVOKED_IN_PROCESS"


def test_register_model_commands_reuses_existing_model_parser() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    model = sub.add_parser("model", help="legacy")
    model_sub = model.add_subparsers(dest="model_command")
    model_sub.add_parser("status").set_defaults(func=lambda _args: 0)
    register_model_commands(sub)
    model_actions = next(
        action for action in model._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert {"status", "list", "route", "health"}.issubset(model_actions.choices)
    args = parser.parse_args(["model", "status"])
    assert setattr(args, "console", Console(json_mode=True)) is None
    assert args.func(args) == 0
    assert "connector" in sub.choices


# --------------------------------------------------------------------------- #
# 8. P1 model platform: tools, streaming and native adapters                   #
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """Minimal urllib response: no sockets, no credentials, no side effects."""

    def __init__(
        self,
        *,
        body: bytes | None = None,
        lines: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body or b""
        self._lines = lines
        self._position = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if self._lines is not None:
            return b"".join(self._lines)
        return self._body if size < 0 else self._body[:size]

    def readline(self, size: int = -1) -> bytes:
        if self._lines is None or self._position >= len(self._lines):
            return b""
        line = self._lines[self._position]
        self._position += 1
        return line if size < 0 else line[:size]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


def _fake_urlopen(monkeypatch, *, body: bytes | None = None, lines: list[bytes] | None = None, headers: dict[str, str] | None = None) -> list[Any]:
    """Replace urllib's opener with a deterministic, in-memory one."""
    calls: list[Any] = []

    def fake(request: Any, timeout: int | None = None) -> _FakeResponse:
        calls.append(request)
        return _FakeResponse(body=body, lines=lines, headers=headers)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _sse(*payloads: Any) -> list[bytes]:
    lines: list[bytes] = []
    for payload in payloads:
        lines.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n")
        lines.append(b"\n")
    return lines


def _openai_request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(
        purpose="test",
        messages=[ChatMessage(role="user", content="hello")],
        **kwargs,
    )


def _openai_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9/v1", model="test-model", api_key="fake-openai-key"
    )


def test_model_contract_extensions_are_additive_and_strict() -> None:
    request = ModelRequest(purpose="test", messages=[])
    assert request.tools is None
    assert request.tool_choice == "auto"
    assert request.stream is False
    assert request.cache_breakpoints == []
    assert _response("p").tool_calls == []
    assert ModelCapabilities(structured_output=True).provider_family == ""
    assert ModelCapabilities(structured_output=True).tool_calling is False
    assert ChatMessage(role="tool", content="result", tool_call_id="c1").role == "tool"
    with pytest.raises(ValidationError):
        ToolDefinition(name="x", description="x", parameters={}, unexpected=True)
    with pytest.raises(ValidationError):
        ToolCall(id="c1", name="x", arguments={}, unexpected=True)


def test_openai_payload_includes_tools_choice_parallel_and_stream() -> None:
    provider = _openai_provider()
    tool = ToolDefinition(
        name="read",
        description="read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    request = ModelRequest(
        purpose="tools",
        messages=[
            ChatMessage(role="system", content="system"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "/etc/hosts"})],
            ),
            ChatMessage(role="tool", content="contents", tool_call_id="c1"),
        ],
        tools=[tool],
        tool_choice="read",
        stream=True,
    )
    payload = provider._build_payload(request, repair_notice=None)
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "read a file",
                "parameters": tool.parameters,
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "read"}}
    assert payload["parallel_tool_calls"] is True
    assert payload["stream"] is True
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"path": "/etc/hosts"}
    )
    assert payload["messages"][2] == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "c1",
    }
    plain = provider._build_payload(_openai_request(), repair_notice=None)
    for key in ("tools", "tool_choice", "parallel_tool_calls", "stream"):
        assert key not in plain
    assert "response_format" not in plain


def test_openai_payload_keeps_strict_json_schema_with_tools() -> None:
    provider = _openai_provider()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    payload = provider._build_payload(
        _openai_request(json_schema=schema, schema_name="answer", tools=[ToolDefinition(name="x")]),
        repair_notice=None,
    )
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "strict": True, "schema": schema},
    }


def test_openai_parse_tool_calls_with_null_content_and_bad_arguments() -> None:
    provider = _openai_provider()
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "bad", "arguments": "not json"},
                        },
                    ],
                }
            }
        ]
    }
    text, structured, calls = provider._parse_response(data, _openai_request())
    assert text == ""
    assert structured is None
    assert calls == [
        ToolCall(id="call_1", name="lookup", arguments={"q": "x"}),
        ToolCall(id="call_2", name="bad", arguments={"_raw": "not json"}),
    ]


def test_openai_generate_parses_tool_calls_usage_and_request_id(monkeypatch) -> None:
    body = _json_body(
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_7",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }
    )
    calls = _fake_urlopen(monkeypatch, body=body, headers={"x-request-id": "req-1"})
    response = _openai_provider().generate(_openai_request())
    assert response.tool_calls == [ToolCall(id="call_7", name="lookup", arguments={"q": "x"})]
    assert response.provider_request_id == "req-1"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 14
    assert calls[0].full_url.endswith("/chat/completions")
    assert calls[0].get_header("Accept") == "application/json"
    assert "fake-openai-key" not in json.dumps(response.model_dump(mode="json"))


def test_openai_sse_stream_assembles_text_tools_usage_and_stops_at_done(monkeypatch) -> None:
    lines = _sse(
        {"id": "chatcmpl-sse", "model": "test-model", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9",
                                "type": "function",
                                "function": {"name": "get_", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "weather", "arguments": '{"city":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"Paris"}'}}
                        ]
                    }
                }
            ]
        },
        {
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            "choices": [],
        },
    )
    lines.append(b"data: [DONE]\n\n")
    lines.extend(_sse({"choices": [{"delta": {"content": "AFTER_DONE"}}]}))
    calls = _fake_urlopen(monkeypatch, lines=lines, headers={"x-request-id": "req-sse"})
    response = _openai_provider().generate(_openai_request(stream=True))
    assert response.text == "Hello"
    assert "AFTER_DONE" not in response.text
    assert response.tool_calls == [
        ToolCall(id="call_9", name="get_weather", arguments={"city": "Paris"})
    ]
    assert response.usage.prompt_tokens == 7
    assert response.usage.completion_tokens == 3
    assert response.provider_request_id == "req-sse"
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent["stream"] is True
    assert calls[0].get_header("Accept") == "text/event-stream"


def test_openai_sse_stream_is_bounded(monkeypatch) -> None:
    huge = b"data: " + b"x" * 70_000 + b"\n\n"
    _fake_urlopen(monkeypatch, lines=[huge])
    with pytest.raises(ModelError) as raised:
        _openai_provider().generate(_openai_request(stream=True))
    assert raised.value.error_class == "protocol"
    assert raised.value.retryable is False


def test_capability_manifests_report_only_implemented_features() -> None:
    openai = _openai_provider()
    caps = openai.capabilities()
    assert caps.streaming is True
    assert caps.tool_calling is True
    assert caps.parallel_tool_calls is True
    assert caps.provider_family == "openai_compatible"
    # Buffered SSE via generate(stream=True) is not the incremental-chunk protocol.
    assert not hasattr(openai, "stream")

    anthropic = AnthropicProvider(model="claude-test", api_key="fake-anthropic-key")
    anthropic_caps = anthropic.capabilities()
    assert anthropic_caps.tool_calling is True
    assert anthropic_caps.prompt_caching is True
    assert anthropic_caps.streaming is False
    assert anthropic_caps.parallel_tool_calls is False
    assert anthropic_caps.provider_family == "anthropic"

    gemini = GeminiProvider(model="gemini-test", api_key="fake-gemini-key")
    gemini_caps = gemini.capabilities()
    assert gemini_caps.tool_calling is True
    assert gemini_caps.streaming is False
    assert gemini_caps.prompt_caching is False
    assert gemini_caps.provider_family == "gemini"


def test_anthropic_tool_use_and_cache_control(monkeypatch) -> None:
    body = _json_body(
        {
            "id": "msg_1",
            "model": "claude-test",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "/etc/hosts"}},
            ],
            "usage": {"input_tokens": 11, "output_tokens": 5},
        }
    )
    calls = _fake_urlopen(monkeypatch, body=body)
    provider = AnthropicProvider(model="claude-test", api_key="anthropic-fake-secret")
    request = ModelRequest(
        purpose="tools",
        messages=[
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="check"),
        ],
        tools=[
            ToolDefinition(
                name="read", description="read a file", parameters={"type": "object"}
            )
        ],
        cache_breakpoints=[0],
    )
    response = provider.generate(request)
    assert response.text == "Let me check."
    assert response.tool_calls == [
        ToolCall(id="toolu_1", name="read", arguments={"path": "/etc/hosts"})
    ]
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 16
    assert response.provider_request_id == "msg_1"
    assert calls[0].full_url.endswith("/v1/messages")
    assert calls[0].get_header("X-api-key") == "anthropic-fake-secret"
    assert calls[0].get_header("Anthropic-version") == "2023-06-01"
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent["system"] == [
        {
            "type": "text",
            "text": "You are helpful.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert sent["tools"] == [
        {
            "name": "read",
            "description": "read a file",
            "input_schema": {"type": "object"},
        }
    ]
    assert "anthropic-fake-secret" not in json.dumps(response.model_dump(mode="json"))

    # Without requested breakpoints there is no cache_control block.
    plain = provider._build_payload(
        ModelRequest(
            purpose="plain",
            messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")],
        ),
        repair_notice=None,
    )
    assert plain["system"] == "sys"


def test_anthropic_stream_request_is_refused_not_downgraded(monkeypatch) -> None:
    provider = AnthropicProvider(model="claude-test", api_key="fake")
    calls = _fake_urlopen(monkeypatch, body=_json_body({"content": []}))
    with pytest.raises(ModelError) as raised:
        provider.generate(_openai_request(stream=True))
    assert raised.value.error_class == "unsupported"
    assert calls == []


def test_gemini_function_call_parsing(monkeypatch) -> None:
    body = _json_body(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Here:"},
                            {"functionCall": {"name": "lookup", "args": {"q": "x"}}},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 2,
                "totalTokenCount": 7,
            },
            "modelVersion": "gemini-test",
        }
    )
    calls = _fake_urlopen(monkeypatch, body=body)
    provider = GeminiProvider(model="models/gemini-test", api_key="gemini-fake-secret")
    request = ModelRequest(
        purpose="tools",
        messages=[
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="find x"),
        ],
        tools=[ToolDefinition(name="lookup", description="look up", parameters={"type": "object"})],
    )
    response = provider.generate(request)
    assert response.text == "Here:"
    assert response.tool_calls == [
        ToolCall(id="gemini_call_1", name="lookup", arguments={"q": "x"})
    ]
    assert response.usage.prompt_tokens == 5
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 7
    assert calls[0].full_url.endswith("/models/gemini-test:generateContent")
    assert calls[0].get_header("X-goog-api-key") == "gemini-fake-secret"
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "lookup",
                    "description": "look up",
                    "parameters": {"type": "object"},
                }
            ]
        }
    ]
    assert sent["systemInstruction"] == {"parts": [{"text": "system"}]}
    assert "gemini-fake-secret" not in json.dumps(response.model_dump(mode="json"))


def test_gemini_unimplemented_options_are_refused(monkeypatch) -> None:
    provider = GeminiProvider(model="gemini-test", api_key="fake")
    calls = _fake_urlopen(monkeypatch, body=_json_body({"candidates": []}))
    with pytest.raises(ModelError) as streamed:
        provider.generate(_openai_request(stream=True))
    assert streamed.value.error_class == "unsupported"
    with pytest.raises(ModelError) as cached:
        provider.generate(_openai_request(cache_breakpoints=[0]))
    assert cached.value.error_class == "unsupported"
    assert calls == []


def test_cli_manifest_reports_capability_values_not_placeholders(tmp_path: Path, monkeypatch) -> None:
    code, payload, _ = _cli(["model", "list"], tmp_path, monkeypatch)
    assert code == 0
    chat = payload["interfaces"]["chat_model"]
    assert chat["streaming"] is True
    assert chat["tool_calling"] is True
    assert chat["parallel_tool_calls"] is True
    assert chat["provider_family"] == "openai_compatible"
    assert "declared_by_adapter" not in json.dumps(payload)
