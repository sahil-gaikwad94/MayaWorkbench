from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mayaworkbench.app.container import Services
from mayaworkbench.config import AppPaths, load_settings, save_settings
from mayaworkbench.models.base import ModelCapabilities, ModelResponse, ModelUsage

ATTENTION_PY = '''"""Attention modules."""

from __future__ import annotations

import math

import torch
from torch import nn

SCALE_EPSILON = 1e-6


class CausalAttention(nn.Module):
    """Multi-head causal self attention.

    The causal mask prevents position t from attending to positions after t.
    """

    def __init__(self, d_model: int, n_heads: int = 4, dropout: float = 0.0):
        super().__init__()
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.d_model = d_model
        self.n_heads = n_heads
        self.head_dim = d_model // n_heads
        self.qkv = nn.Linear(d_model, 3 * d_model)
        self.out = nn.Linear(d_model, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, seq, dim = x.shape
        qkv = self.qkv(x)
        q, k, v = qkv.chunk(3, dim=-1)
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.head_dim)
        mask = torch.triu(torch.ones(seq, seq, device=x.device), diagonal=1).bool()
        scores = scores.masked_fill(mask, float("-inf"))
        weights = torch.softmax(scores, dim=-1)
        context = torch.matmul(self.dropout(weights), v)
        return self.out(context)


def build_attention(d_model: int) -> CausalAttention:
    """Construct the default attention module."""
    return CausalAttention(d_model=d_model, n_heads=4)
'''

MODEL_PY = '''"""Model assembly."""

from __future__ import annotations

import torch
from torch import nn

from attention import CausalAttention


class Block(nn.Module):
    def __init__(self, d_model: int):
        super().__init__()
        self.attn = CausalAttention(d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.attn(x)


class Model(nn.Module):
    """Stacked causal blocks."""

    def __init__(self, vocab_size: int, d_model: int = 32):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, d_model)
        self.block = Block(d_model)
        self.head = nn.Linear(d_model, vocab_size)

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        hidden = self.block(self.embed(tokens))
        return self.head(hidden)
'''

UTILS_PY = '''"""Utility helpers."""

import re

TOKEN_PATTERN = re.compile(r"\\w+")


def count_tokens(text: str) -> int:
    """Count whitespace-delimited tokens."""
    return len(TOKEN_PATTERN.findall(text))
'''

TEST_PY = '''import torch

from attention import CausalAttention


def test_forward_shape():
    module = CausalAttention(d_model=8, n_heads=2)
    out = module(torch.randn(1, 4, 8))
    assert out.shape == (1, 4, 8)
'''

README = """# TinyFixture

A deliberately small transformer fixture used by the MayaWorkbench tests.

The attention module lives in `attention.py` and must never attend to future
positions.
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture-repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    git(root, "config", "commit.gpgsign", "false")
    write(root / ".gitignore", ".venv/\n*.log\n")
    write(root / "attention.py", ATTENTION_PY)
    write(root / "model.py", MODEL_PY)
    write(root / "utils.py", UTILS_PY)
    write(root / "tests" / "test_attention.py", TEST_PY)
    write(root / "README.md", README)
    write(root / ".env", "API_KEY=sk-not-a-real-key-0123456789abcdef0123456789\n")
    write(root / "debug.log", "ignored build log\n")
    (root / "weights.bin").write_bytes(b"\x00\x01\x02binary\x00data")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Initial fixture")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def services(home: Path):
    service = Services.open(str(home))
    try:
        yield service
    finally:
        service.close()


@pytest.fixture
def project(services: Services):
    return services.create_project("Fixture Project", "test project")


@pytest.fixture
def indexed(services: Services, project, repo: Path):
    attached = services.repository_service.attach(project, repo)
    outcome = services.repository_service.create_snapshot(attached.repository)
    report = services.indexer.index(outcome.snapshot.id)
    return SimpleNamespace(
        services=services,
        project=project,
        repository=attached.repository,
        snapshot=outcome.snapshot,
        report=report,
        repo=repo,
    )


class FakeProvider:
    """A provider double that answers from the supplied context."""

    name = "fake"
    model = "fake-1"

    def __init__(self, mode: str = "valid"):
        self.mode = mode
        self.calls = 0

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(structured_output=True)

    def generate(self, request):
        self.calls += 1
        user_text = request.messages[-1].content
        blocks = re.findall(
            r"### FILE (.+?) \(lines (\d+)-(\d+)\)\nreason: .*?\n```\n(.*?)\n```",
            user_text,
            flags=re.S,
        )
        if self.mode == "bad_schema":
            payload = {"answer_markdown": 7, "citations": [], "uncertainties": [], "unsupported_claims": []}
        elif self.mode == "mixed" and blocks:
            path, start, end, body = blocks[0]
            first_line = next(
                (line.split(": ", 1)[1] for line in body.splitlines() if ": " in line),
                "",
            )
            payload = {
                "answer_markdown": "One real citation and one invented one.",
                "citations": [
                    {
                        "path": path,
                        "start_line": int(start),
                        "end_line": int(end),
                        "quote": first_line,
                        "reason": "real",
                    },
                    {
                        "path": "invented/module.py",
                        "start_line": 1,
                        "end_line": 2,
                        "quote": "def fabricated():",
                        "reason": "invented",
                    },
                ],
                "uncertainties": [],
                "unsupported_claims": [],
            }
        elif self.mode == "invalid_path" or not blocks:
            payload = {
                "answer_markdown": "The code does something.",
                "citations": [
                    {
                        "path": "does/not/exist.py",
                        "start_line": 1,
                        "end_line": 2,
                        "quote": "def invented():",
                        "reason": "made up",
                    }
                ],
                "uncertainties": [],
                "unsupported_claims": [],
            }
        else:
            path, start, end, body = blocks[0]
            first_line = next(
                (line.split(": ", 1)[1] for line in body.splitlines() if ": " in line),
                "",
            )
            payload = {
                "answer_markdown": (
                    f"`{path}` contains the relevant definition around line {start}."
                ),
                "citations": [
                    {
                        "path": path,
                        "start_line": int(start),
                        "end_line": int(end),
                        "quote": first_line,
                        "reason": "definition",
                    }
                ],
                "uncertainties": [],
                "unsupported_claims": [],
            }
        return ModelResponse(
            text=json.dumps(payload),
            structured=payload,
            usage=ModelUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            provider=self.name,
            model=self.model,
            provider_request_id="fake-request",
            latency_ms=5,
            used_structured_output=True,
        )


def enable_virtual_model(home: Path) -> None:
    """Configure a fake provider so policy checks pass without network access."""
    paths = AppPaths.resolve(str(home))
    settings = load_settings(paths)
    settings = settings.model_copy(
        update={
            "data_policy": "BYOK_REMOTE_MODEL",
            "model": settings.model.model_copy(
                update={
                    "provider": "openai_compatible",
                    "model": "fake-1",
                    "base_url": "http://127.0.0.1:9/v1",
                    "api_key_env": "MAYA_MODEL_API_KEY",
                }
            ),
        }
    )
    save_settings(paths, settings)


@pytest.fixture
def virtual_model(services, home: Path, monkeypatch):
    enable_virtual_model(home)
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")
    provider = FakeProvider()

    def factory(_settings):
        return provider

    services.settings = load_settings(AppPaths.resolve(str(home)))
    from mayaworkbench.models.gateway import ModelGateway

    services.gateway = ModelGateway(services.settings, services.invocations, services.events, factory)
    services.ask.gateway = services.gateway
    services.ask.settings = services.settings
    return provider
