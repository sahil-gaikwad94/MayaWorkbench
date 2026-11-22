"""Optional semantic adapters are deterministic and measured against a lexical baseline."""

from __future__ import annotations

import json

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.retrieval.semantic import (
    ApiEmbeddingError,
    ApiEmbeddingProvider,
    EmbeddingProvider,
    HashingEmbeddingModel,
    HybridRetriever,
    LexicalReranker,
    evaluate_hybrid,
    ppr_scores,
    reciprocal_rank_fusion,
)

CASES = [
    {
        "query": "causal attention masking",
        "candidates": [
            {"name": "README.md", "score": 0.9},
            {"name": "causal_mask.py", "score": 0.2},
        ],
        "relevant": "causal_mask.py",
    },
    {
        "query": "gradient finite difference check",
        "candidates": [
            {"name": "notes.txt", "score": 0.8},
            {"name": "gradient_check.py", "score": 0.1},
        ],
        "relevant": "gradient_check.py",
    },
]


def test_hashing_embeddings_are_deterministic_and_bounded():
    model = HashingEmbeddingModel()
    first = model.embed("causal attention mask")
    assert first == model.embed("causal attention mask")
    assert len(first) == model.dim
    assert 0.0 <= model.similarity("causal mask", "causal mask") <= 1.0


def test_reranker_beats_baseline_on_gold_set():
    result = evaluate_hybrid(CASES)
    assert result["cases"] == 2
    assert result["hybrid_hit_at_k"] >= result["baseline_hit_at_k"]
    assert "not an external benchmark" in result["note"]


def test_ppr_is_damped_and_normalised():
    scores = ppr_scores({"a": ["b"], "b": ["c"], "c": ["a"]}, ["a"])
    assert set(scores) == {"a", "b", "c"}
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    assert scores["a"] > scores["c"]


def test_semantic_cli_reports_evaluation(tmp_path, capsys):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    base = ["--home", str(tmp_path / "home"), "--json"]
    assert main(base + ["init", "--project", "semantic"]) == 0
    capsys.readouterr()
    code = main(base + ["semantic", "evaluate", str(cases)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and "baseline_hit_at_k" in payload


# ---------------------------------------------------------------------------
# Pluggable embedding providers and rank fusion (P2, all opt-in).
# ---------------------------------------------------------------------------
def test_hashing_provider_is_labelled_lexical_not_semantic():
    model = HashingEmbeddingModel(dim=32)
    assert isinstance(model, EmbeddingProvider)
    description = model.describe()
    assert description["mode"] == "LOCAL_DETERMINISTIC"
    assert description["semantic"] is False
    assert description["dimension"] == 32
    assert description["kind"] == "lexical_hashing"
    assert "not semantic" in description["note"]


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.closed = False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def close(self) -> None:
        self.closed = True


def test_api_embedding_provider_batches_vectors_and_reports_metadata():
    captured: dict = {}
    secret = "sk-test-SECRET-123"

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ],
                "model": "fake-embed-v2",
            }
        )

    provider = ApiEmbeddingProvider(
        endpoint="https://example.test/v1/embeddings",
        api_key=lambda: secret,
        model="fake-embed",
        dimension=3,
        timeout=1.5,
        opener=opener,
    )
    vectors = provider.embed_batch(["b", "a"])
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert provider.dimension == 3
    assert captured["timeout"] == 1.5
    assert captured["body"]["input"] == ["b", "a"]
    assert captured["body"]["dimensions"] == 3
    assert captured["request"].get_header("Authorization") == f"Bearer {secret}"
    assert provider.embed("a") == [1.0, 0.0, 0.0]

    description = provider.describe()
    assert description["semantic"] is True
    assert description["mode"] == "REMOTE_API"
    assert description["response_model"] == "fake-embed-v2"
    assert description["dimension"] == 3
    assert secret not in json.dumps(description)


def test_api_embedding_provider_fails_closed_and_redacts_key():
    secret = "sk-super-secret-KEY"

    def broken_opener(request, *, timeout):
        raise RuntimeError(f"upstream rejected {secret}")

    provider = ApiEmbeddingProvider(
        endpoint="https://example.test/v1/embeddings",
        api_key=lambda: secret,
        opener=broken_opener,
    )
    with pytest.raises(ApiEmbeddingError) as error:
        provider.embed("hello")
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)

    no_key = ApiEmbeddingProvider(
        endpoint="https://example.test/v1/embeddings",
        api_key=lambda: "",
        opener=broken_opener,
    )
    with pytest.raises(ApiEmbeddingError, match="not configured"):
        no_key.embed("hello")


def test_api_embedding_provider_rejects_malformed_vectors():
    def bad_opener(request, *, timeout):
        return _FakeResponse({"data": [{"index": 0, "embedding": [1.0, "nope"]}]})

    provider = ApiEmbeddingProvider(
        endpoint="https://example.test/v1/embeddings",
        api_key=lambda: "sk-test",
        dimension=2,
        opener=bad_opener,
    )
    with pytest.raises(ApiEmbeddingError):
        provider.embed("hello")


def test_rrf_records_contributing_channels_and_respects_weights():
    fused = reciprocal_rank_fusion(
        {"bm25": ["a", "b", "c"], "ppr": ["c", "a", "b"]},
        weights={"bm25": 1.0, "ppr": 2.0},
    )
    assert [row["id"] for row in fused] == ["c", "a", "b"]
    top = fused[0]
    assert top["channels"] == ["bm25", "ppr"]
    assert top["ranks"] == {"bm25": 3, "ppr": 1}
    assert all("channels" in row for row in fused)


def test_missing_dense_channel_does_not_change_bm25_ppr_order():
    retriever = HybridRetriever()
    bm25, ppr = ["a", "b", "c"], ["c", "a", "b"]
    without = retriever.fuse(bm25=bm25, ppr=ppr)
    explicit_none = retriever.fuse(bm25=bm25, ppr=ppr, dense=None)
    assert without == explicit_none
    assert all("dense" not in row["channels"] for row in without)


def test_hybrid_retriever_uses_fake_dense_provider_offline():
    class FakeProvider:
        name = "fake"
        mode = "OFFLINE_FAKE"
        dimension = 2

        def embed(self, text: str) -> list[float]:
            return [1.0, 0.0] if "cat" in text else [0.0, 1.0]

        def describe(self) -> dict:
            return {"name": self.name, "mode": self.mode, "semantic": False}

    retriever = HybridRetriever(dense_provider=FakeProvider())
    ranks = retriever.dense_ranks("cat", {"cat.txt": "a cat", "dog.txt": "a dog"})
    assert ranks == ["cat.txt", "dog.txt"]
    fused = retriever.fuse(bm25=["dog.txt"], ppr=["dog.txt"], dense=ranks)
    assert fused[0]["id"] == "dog.txt"
    assert any(row["channels"] == ["dense"] for row in fused)
