"""Repository map.

A deterministic summary derived entirely from the index — no model involved.
It answers "what is in this repository, and how well do we understand it?" with
counts, entry points, language coverage, and explicit parser limitations.
"""

from __future__ import annotations

import tomllib
from typing import Any

from pydantic import BaseModel, Field

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.models import Repository, RepositorySnapshot
from mayaworkbench.errors import NotFoundError
from mayaworkbench.indexing.languages import (
    LANGUAGES_WITH_AST,
    LANGUAGES_WITH_HEURISTIC_SYMBOLS,
)
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    FileStore,
    IssueStore,
    RepositoryStore,
    SnapshotStore,
    SymbolStore,
)

PACKAGE_MANAGER_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "pyproject"),
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("requirements.txt", "pip-requirements"),
    ("setup.py", "setuptools"),
    ("setup.cfg", "setuptools-config"),
    ("Pipfile", "pipenv"),
    ("environment.yml", "conda"),
    ("environment.yaml", "conda"),
    ("Makefile", "make"),
    ("Dockerfile", "docker"),
    ("CMakeLists.txt", "cmake"),
    ("package.json", "npm"),
    ("Cargo.toml", "cargo"),
    ("go.mod", "go-modules"),
)

CONVENTIONAL_ENTRY_FILES = {
    "main.py": "conventional main module",
    "__main__.py": "package __main__ module",
    "manage.py": "framework management entry point",
    "cli.py": "conventional CLI module",
}


class MapSymbol(BaseModel):
    path: str
    qualified_name: str
    kind: str
    signature: str
    start_line: int
    end_line: int
    docstring_first_line: str | None = None


class MapEntryPoint(BaseModel):
    path: str
    kind: str
    detail: str


class MapLanguage(BaseModel):
    language: str
    files: int
    lines: int
    bytes: int


class MapDirectory(BaseModel):
    directory: str
    files: int
    lines: int


class RepositoryMap(BaseModel):
    snapshot_id: str
    repository_id: str
    repository_name: str
    repository_root: str
    kind: str
    status: str
    commit_sha: str | None
    branch: str | None
    dirty: bool
    includes_worktree_patch: bool
    patch_hash: str | None
    indexer_version: str
    indexed_at: str | None
    generated_at: str
    files: int
    files_skipped: int
    bytes_indexed: int
    lines_indexed: int
    symbols: int
    symbol_kinds: dict[str, int] = Field(default_factory=dict)
    edges: int = 0
    edges_resolved: int = 0
    edges_unresolved: int = 0
    languages: list[MapLanguage] = Field(default_factory=list)
    directories: list[MapDirectory] = Field(default_factory=list)
    parse_coverage: dict[str, int] = Field(default_factory=dict)
    ast_languages: list[str] = Field(default_factory=list)
    lexical_only_languages: list[str] = Field(default_factory=list)
    heuristic_languages: list[str] = Field(default_factory=list)
    entry_points: list[MapEntryPoint] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    documentation_files: list[str] = Field(default_factory=list)
    public_symbols: list[MapSymbol] = Field(default_factory=list)
    issues: dict[str, int] = Field(default_factory=dict)
    unresolved_imports: int = 0
    limitations: list[str] = Field(default_factory=list)


class RepositoryMapService:
    def __init__(
        self,
        db: Database,
        *,
        snapshots: SnapshotStore,
        repositories: RepositoryStore,
        files: FileStore,
        symbols: SymbolStore,
        issues: IssueStore,
    ):
        self.db = db
        self.snapshots = snapshots
        self.repositories = repositories
        self.files = files
        self.symbols = symbols
        self.issues = issues

    def build(self, snapshot_id: str) -> RepositoryMap:
        snapshot = self.snapshots.get(snapshot_id)
        repository = self.repositories.get(snapshot.repository_id)
        if snapshot.indexed_at is None:
            raise NotFoundError(
                f"Snapshot {snapshot_id} has not been indexed",
                hint="Run `maya index --snapshot <id>` first.",
            )
        return self.build_from(snapshot, repository)

    def build_from(
        self, snapshot: RepositorySnapshot, repository: Repository
    ) -> RepositoryMap:
        languages = [
            MapLanguage(
                language=str(row["language"]),
                files=int(row["files"]),
                lines=int(row["lines"] or 0),
                bytes=int(row["bytes"] or 0),
            )
            for row in self.files.language_stats(snapshot.id)
        ]
        directories = [
            MapDirectory(
                directory=str(row["dir"]),
                files=int(row["files"]),
                lines=int(row["lines"] or 0),
            )
            for row in self.files.directory_stats(snapshot.id)
        ]
        indexed = self.files.iter_files(snapshot.id)
        tests = sorted(file.path for file in indexed if file.is_test)
        configs = sorted(file.path for file in indexed if file.is_config)
        docs = sorted(file.path for file in indexed if file.is_doc)
        coverage: dict[str, int] = {}
        for file in indexed:
            coverage[file.parse_coverage.value] = coverage.get(file.parse_coverage.value, 0) + 1

        metadata = snapshot.index_metadata or {}
        symbol_kinds = self.symbols.count_by_kind(snapshot.id)
        edges = int(metadata.get("edges", 0) or 0)
        edges_resolved = int(metadata.get("edges_resolved", 0) or 0)
        edges_unresolved = int(metadata.get("edges_unresolved", 0) or 0)
        if not edges:
            counts = self.symbols.edge_confidence_counts(snapshot.id)
            edges = sum(counts.values())
            edges_unresolved = counts.get("LOW", 0)
            edges_resolved = edges - edges_unresolved

        ast_languages = sorted(
            {row.language for row in languages if row.language in LANGUAGES_WITH_AST}
        )
        heuristic_languages = sorted(
            {row.language for row in languages if row.language in LANGUAGES_WITH_HEURISTIC_SYMBOLS}
        )
        lexical_only = sorted(
            {
                file.language
                for file in indexed
                if file.language not in LANGUAGES_WITH_AST
                and file.language not in LANGUAGES_WITH_HEURISTIC_SYMBOLS
                and file.language not in ("unknown",)
            }
        )
        limitations = [
            "Python is parsed with the standard-library AST; JavaScript/TypeScript/Go/Rust/"
            "C/C++/CUDA use bounded heuristic declaration discovery with LOW-confidence symbols and "
            "HEURISTIC relation provenance; every other language is lexical-only.",
            "Static call graphs are incomplete in a dynamic language; unresolved edges are "
            "reported with LOW confidence rather than omitted.",
        ]
        if snapshot.kind.value == "committed" and snapshot.dirty:
            limitations.append(
                "The working tree had uncommitted changes when this snapshot was pinned; they "
                "are not part of the indexed content."
            )

        return RepositoryMap(
            snapshot_id=snapshot.id,
            repository_id=repository.id,
            repository_name=repository.name,
            repository_root=repository.root_path,
            kind=snapshot.kind.value,
            status=snapshot.status.value,
            commit_sha=snapshot.commit_sha,
            branch=snapshot.branch,
            dirty=snapshot.dirty,
            includes_worktree_patch=snapshot.includes_worktree_patch,
            patch_hash=snapshot.patch_hash,
            indexer_version=snapshot.indexer_version,
            indexed_at=snapshot.indexed_at.isoformat() if snapshot.indexed_at else None,
            generated_at=utcnow().isoformat(),
            files=len(indexed),
            files_skipped=int(metadata.get("files_skipped", 0) or 0),
            bytes_indexed=sum(file.size_bytes for file in indexed),
            lines_indexed=sum(file.line_count for file in indexed),
            symbols=sum(symbol_kinds.values()),
            symbol_kinds=symbol_kinds,
            edges=edges,
            edges_resolved=edges_resolved,
            edges_unresolved=edges_unresolved,
            languages=languages,
            directories=directories,
            parse_coverage=coverage,
            ast_languages=ast_languages,
            lexical_only_languages=lexical_only,
            heuristic_languages=heuristic_languages,
            entry_points=self._entry_points(snapshot, indexed),
            package_managers=self._package_managers(indexed),
            test_files=tests[:200],
            config_files=configs[:200],
            documentation_files=docs[:200],
            public_symbols=self._public_symbols(snapshot.id),
            issues=self.issues.counts_by_kind(snapshot.id),
            unresolved_imports=edges_unresolved,
            limitations=limitations,
        )

    # ------------------------------------------------------------------ pieces
    def _entry_points(self, snapshot: RepositorySnapshot, indexed) -> list[MapEntryPoint]:  # type: ignore[no-untyped-def]
        by_path = {file.path: file for file in indexed}
        entries: dict[str, MapEntryPoint] = {}
        recorded = snapshot.index_metadata.get("main_guard_paths")
        main_guard_paths = (
            [str(path) for path in recorded]
            if isinstance(recorded, list)
            else self.files.main_guard_paths(snapshot.id)
        )
        for path in main_guard_paths:
            entries[path] = MapEntryPoint(
                path=path, kind="main_guard", detail="executes under `if __name__ == '__main__'`"
            )
        for path, file in by_path.items():
            if file.path.rsplit("/", 1)[-1] in CONVENTIONAL_ENTRY_FILES and path not in entries:
                entries[path] = MapEntryPoint(
                    path=path,
                    kind="convention",
                    detail=CONVENTIONAL_ENTRY_FILES[file.path.rsplit("/", 1)[-1]],
                )
        pyproject = by_path.get("pyproject.toml")
        if pyproject is not None:
            entries.update(self._pyproject_scripts(pyproject))
        return sorted(entries.values(), key=lambda entry: entry.path)

    def _pyproject_scripts(self, file) -> dict[str, MapEntryPoint]:  # type: ignore[no-untyped-def]
        text = self.files.read_text(file.id)
        try:
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            return {}
        scripts: dict[str, Any] = {}
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("scripts"), dict):
            scripts.update(project["scripts"])
        poetry = data.get("tool", {}).get("poetry") if isinstance(data.get("tool"), dict) else None
        if isinstance(poetry, dict) and isinstance(poetry.get("scripts"), dict):
            scripts.update(poetry["scripts"])
        return {
            f"pyproject.toml#{name}": MapEntryPoint(
                path="pyproject.toml",
                kind="console_script",
                detail=f"{name} = {target}",
            )
            for name, target in scripts.items()
            if isinstance(target, str)
        }

    def _package_managers(self, indexed) -> list[str]:  # type: ignore[no-untyped-def]
        paths = {file.path for file in indexed}
        found = [label for marker, label in PACKAGE_MANAGER_MARKERS if marker in paths]
        return sorted(set(found))

    def _public_symbols(self, snapshot_id: str, limit: int = 150) -> list[MapSymbol]:
        symbols = self.symbols.top_level_symbols(snapshot_id, limit=limit * 4)
        public = [symbol for symbol in symbols if not symbol.name.startswith("_")]
        selected = public[:limit]
        result: list[MapSymbol] = []
        for symbol in selected:
            first_line = None
            if symbol.docstring:
                first_line = symbol.docstring.splitlines()[0].strip()[:200]
            result.append(
                MapSymbol(
                    path=symbol.path,
                    qualified_name=symbol.qualified_name,
                    kind=symbol.kind.value,
                    signature=symbol.signature,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    docstring_first_line=first_line,
                )
            )
        return result


__all__ = [
    "RepositoryMap",
    "RepositoryMapService",
    "MapEntryPoint",
    "MapLanguage",
    "MapDirectory",
    "MapSymbol",
]
