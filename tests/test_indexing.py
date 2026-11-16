from __future__ import annotations

import json
import sys

import pytest

from mayaworkbench.domain.enums import (
    EdgeConfidence,
    EdgeType,
    FileStatus,
    IssueKind,
    ParseCoverage,
    SnapshotStatus,
    SymbolKind,
)
from mayaworkbench.errors import IndexingError
from mayaworkbench.indexing.python_parser import parse_python

from .conftest import git, write


def test_python_symbols_have_exact_spans_and_signatures(indexed):
    services = indexed.services
    snapshot = indexed.snapshot
    attention = services.files.get_by_path(snapshot.id, "attention.py")
    assert attention is not None
    assert attention.status == FileStatus.PARSED
    assert attention.parse_coverage == ParseCoverage.FULL
    assert attention.language == "python"

    classes = services.symbols.find_by_qualified(snapshot.id, "CausalAttention")
    assert classes and classes[0].kind == SymbolKind.CLASS
    assert classes[0].start_line == 13
    assert "class CausalAttention(nn.Module)" in classes[0].signature.replace("class ", "class ")

    methods = services.symbols.find_by_qualified(snapshot.id, "CausalAttention.forward")
    assert methods and methods[0].kind == SymbolKind.METHOD
    assert methods[0].parent_symbol_id == classes[0].id
    source_lines = (indexed.repo / "attention.py").read_text(encoding="utf-8").splitlines()
    span = "\n".join(source_lines[methods[0].start_line - 1 : methods[0].end_line])
    assert "def forward" in span
    assert "return self.out(context)" in span

    constants = services.symbols.find_by_qualified(snapshot.id, "SCALE_EPSILON")
    assert constants and constants[0].kind == SymbolKind.CONSTANT


def test_docstrings_and_decorator_extraction(services, project, repo):
    write(
        repo / "decorated.py",
        '''"""Module docs."""

import functools


def logged(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


@logged
def compute(x):
    """Compute something."""
    return x + 1
''',
    )
    from .conftest import git

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add decorated module")
    repository = services.repository_service.attach(project, repo).repository
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(snapshot.id)

    compute = services.symbols.find_by_qualified(snapshot.id, "compute")[0]
    assert compute.decorators == ["logged"]
    assert compute.docstring == "Compute something."
    edges = services.symbols.edges_from(compute.id, EdgeType.DECORATES.value)
    assert edges and edges[0]["target_name"] == "logged"


def test_imports_and_calls_resolve_with_confidence(indexed):
    services = indexed.services
    snapshot = indexed.snapshot

    causal = services.symbols.find_by_qualified(snapshot.id, "CausalAttention")[0]
    block_init = services.symbols.find_by_qualified(snapshot.id, "Block.__init__")[0]
    calls = services.symbols.edges_from(block_init.id, EdgeType.CALLS.value)
    resolved = [edge for edge in calls if edge["target_symbol_id"] == causal.id]
    assert resolved, "imported symbol call should resolve to the class definition"
    assert resolved[0]["confidence"] == EdgeConfidence.HIGH.value

    build = services.symbols.find_by_qualified(snapshot.id, "build_attention")[0]
    same_file_calls = services.symbols.edges_from(build.id, EdgeType.CALLS.value)
    assert any(edge["target_symbol_id"] == causal.id for edge in same_file_calls)

    imports = services.symbols.edges_from(
        services.symbols.find_by_qualified(snapshot.id, "model")[0].id,
        EdgeType.IMPORTS.value,
    )
    assert any(edge["target_file_id"] for edge in imports), "local import should resolve to a file"


def test_external_calls_stay_unresolved_with_low_confidence(indexed):
    services = indexed.services
    snapshot = indexed.snapshot
    forward = services.symbols.find_by_qualified(snapshot.id, "CausalAttention.forward")[0]
    calls = services.symbols.edges_from(forward.id, EdgeType.CALLS.value)
    external = [edge for edge in calls if edge["target_name"].startswith("torch.")]
    assert external
    assert all(edge["target_symbol_id"] is None for edge in external)
    assert all(edge["confidence"] == EdgeConfidence.LOW.value for edge in external)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax needs Python 3.12")
def test_pep695_type_alias_is_a_symbol():
    parsed = parse_python("type Alias = dict[str, int]\n", module_name="m", path="m.py")
    aliases = [s for s in parsed.symbols if s.kind == SymbolKind.TYPE_ALIAS]
    assert len(aliases) == 1
    assert aliases[0].name == "Alias"
    assert aliases[0].signature == "type Alias = dict[str, int]"


def test_parse_error_is_lexical_only_and_reported(services, project, repo):
    write(repo / "broken.py", "def broken(:\n    pass\n")
    from .conftest import git

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add broken module")
    repository = services.repository_service.attach(project, repo).repository
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    report = services.indexer.index(snapshot.id)

    record = services.files.get_by_path(snapshot.id, "broken.py")
    assert record is not None
    assert record.status == FileStatus.LEXICAL_ONLY
    assert record.parse_coverage == ParseCoverage.PARTIAL
    issues = services.issues.list(snapshot.id, kind=IssueKind.PARSE_ERROR)
    assert any(issue.path == "broken.py" for issue in issues)
    hits, _ = services.search.search_text(snapshot.id, "broken", mode="lexical")
    assert hits, "unparseable files must still be lexically searchable"
    assert report.issues >= 1


def test_secrets_and_excluded_files_are_not_indexed(indexed):
    services = indexed.services
    snapshot = indexed.snapshot
    paths = {record.path for record in services.files.iter_files(snapshot.id)}

    assert ".env" not in paths
    assert "weights.bin" not in paths
    assert "debug.log" not in paths  # gitignored, so absent from the committed tree
    kinds = services.issues.counts_by_kind(snapshot.id)
    assert kinds.get(IssueKind.SKIPPED_SECRET.value, 0) >= 1
    assert kinds.get(IssueKind.EXCLUDED_GLOB.value, 0) >= 1


def test_manifest_artifact_matches_indexed_hashes(indexed):
    services = indexed.services
    manifest = json.loads(services.artifacts.read_text(indexed.report.manifest_artifact_id))
    entries = {entry["path"]: entry for entry in manifest["files"]}
    record = services.files.get_by_path(indexed.snapshot.id, "attention.py")
    assert entries["attention.py"]["sha256"] == record.content_sha256
    assert manifest["commit_sha"] == indexed.snapshot.commit_sha
    assert manifest["indexer_version"] == indexed.snapshot.indexer_version


def test_reindexing_is_idempotent(indexed):
    services = indexed.services
    before = services.files.status_counts(indexed.snapshot.id)
    services.indexer.index(indexed.snapshot.id)
    after = services.files.status_counts(indexed.snapshot.id)
    assert before == after
    assert services.files.get_by_path(indexed.snapshot.id, "attention.py") is not None


def test_failed_reindex_preserves_previous_index(indexed, monkeypatch):
    services = indexed.services
    from mayaworkbench.indexing.resolve import EdgeResolver

    def explode(self):
        raise RuntimeError("resolution exploded")

    monkeypatch.setattr(EdgeResolver, "resolve_all", explode)
    with pytest.raises(IndexingError, match="resolution exploded"):
        services.indexer.index(indexed.snapshot.id)

    snapshot = services.snapshots.get(indexed.snapshot.id)
    assert snapshot.status == SnapshotStatus.INDEXED
    assert snapshot.error and "resolution exploded" in snapshot.error
    assert services.files.get_by_path(snapshot.id, "attention.py") is not None
    events = [event.event_type.value for event in services.events.list(snapshot_id=snapshot.id, limit=20)]
    assert "INDEX_FAILED" in events


def test_package_relative_and_absolute_imports_resolve(services, project, repo):
    """Regression: alias rows stored level/is_from in the wrong order."""
    write(repo / "pkg" / "__init__.py", 'from .core import use\n')
    write(repo / "pkg" / "helpers.py", "def helper():\n    return 1\n")
    write(
        repo / "pkg" / "core.py",
        "from .helpers import helper\n"
        "from pkg.helpers import helper as again\n"
        "\n\n"
        "def use():\n"
        "    return helper() + again()\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add package")
    repository = services.repository_service.attach(project, repo).repository
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(snapshot.id)

    helper = services.symbols.find_by_qualified(snapshot.id, "helper")[0]
    use = services.symbols.find_by_qualified(snapshot.id, "use")[0]
    calls = services.symbols.edges_from(use.id, EdgeType.CALLS.value)
    targeted = [edge for edge in calls if edge["target_symbol_id"] == helper.id]
    assert len(targeted) == 2, [
        (edge["target_name"], edge["target_symbol_id"], edge["confidence"]) for edge in calls
    ]
    assert all(edge["confidence"] == EdgeConfidence.HIGH.value for edge in targeted)


def test_same_file_name_is_only_resolved_when_visible(services, project, repo):
    write(
        repo / "scopes.py",
        "def outer():\n"
        "    def helper():\n"
        "        return 1\n"
        "\n"
        "    return helper()\n"
        "\n\n"
        "def unrelated():\n"
        "    return helper()\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add scopes")
    repository = services.repository_service.attach(project, repo).repository
    snapshot = services.repository_service.create_snapshot(repository).snapshot
    services.indexer.index(snapshot.id)

    outer = services.symbols.find_by_qualified(snapshot.id, "outer")[0]
    nested = services.symbols.find_by_qualified(snapshot.id, "outer.helper")[0]
    inner_calls = services.symbols.edges_from(outer.id, EdgeType.CALLS.value)
    assert any(edge["target_symbol_id"] == nested.id for edge in inner_calls)

    unrelated = services.symbols.find_by_qualified(snapshot.id, "unrelated")[0]
    outer_scope_calls = services.symbols.edges_from(unrelated.id, EdgeType.CALLS.value)
    helper_edges = [edge for edge in outer_scope_calls if edge["target_name"] == "helper"]
    assert helper_edges
    assert all(edge["target_symbol_id"] is None for edge in helper_edges), (
        "a function nested inside another function is not visible by bare name"
    )


def test_clean_worktree_snapshot_is_committed(services, project, repo):
    repository = services.repository_service.attach(project, repo).repository
    outcome = services.repository_service.create_snapshot(repository, working_tree="include")
    assert outcome.snapshot.kind.value == "committed"
    assert outcome.snapshot.includes_worktree_patch is False
    assert any("working tree is clean" in warning for warning in outcome.warnings)


def test_reversed_main_guard_is_detected():
    parsed = parse_python(
        'if "__main__" == __name__:\n    print("run")\n', module_name="entry", path="entry.py"
    )
    assert parsed.has_main_guard is True


def test_tests_are_classified(indexed):
    services = indexed.services
    record = services.files.get_by_path(indexed.snapshot.id, "tests/test_attention.py")
    assert record is not None and record.is_test is True
    readme = services.files.get_by_path(indexed.snapshot.id, "README.md")
    assert readme is not None and readme.is_doc is True
