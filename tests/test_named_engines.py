"""Named v2.0 engines exist as honest facades over the real distributed services."""

from __future__ import annotations

from mayaworkbench.experiments.auditor import ExperimentLabAuditor
from mayaworkbench.indexing.engine import RepoIntelligenceEngine
from mayaworkbench.mapping.matrix_engine import PaperToCodeMatrixEngine


def test_repo_intelligence_engine_facade(services, indexed):
    engine = RepoIntelligenceEngine(
        indexer=services.indexer,
        search=services.search,
        retrieval=services.retrieval,
    )
    report = engine.index(indexed.snapshot.id)
    assert report.files_indexed > 0
    bundle = engine.retrieve(indexed.snapshot.id, "count_tokens")
    assert bundle.context.has_evidence()
    assert isinstance(engine.search_text(indexed.snapshot.id, "count_tokens"), list)


def test_paper_to_code_matrix_engine_facade(services, indexed):
    engine = PaperToCodeMatrixEngine(
        mapping=services.mapping_service, claims=services.claim_service
    )
    matrix = engine.build_matrix(indexed.project, indexed.snapshot)
    assert "rows" in matrix
    assert "PAPER_METHOD" in engine.claim_types()


def test_experiment_lab_auditor_facade(services):
    auditor = ExperimentLabAuditor(services.experiment_service)
    summary = auditor.statistics_summary([1.0, 2.0, 3.0])
    assert summary["n"] == 3
    assert summary["mean"] == 2.0
    assert "no p-value" in summary["note"]
