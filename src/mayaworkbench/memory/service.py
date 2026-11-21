"""Durable project memory (§15).

Design rules enforced here:

* every item names its author (user, model, system) and its provenance;
* a model-authored item starts ``UNCONFIRMED`` and can only be confirmed by a
  user action — repetition never promotes it;
* a commit-scoped item becomes ``STALE`` when the repository moves past the
  revision it described;
* a negative result is scoped to the protocol and revision that produced it;
  it is never stored as a general prohibition;
* obvious secret material is refused instead of being written to the database;
* provenance references are checked against real records, so a memory cannot
  cite an experiment or artifact that does not exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import ActorKind, EventType, MemoryAuthor, MemoryKind, MemoryStatus
from mayaworkbench.domain.models import MemorySource, ProjectMemory, WorkflowEvent
from mayaworkbench.errors import ProjectMemoryError
from mayaworkbench.ids import new_ulid
from mayaworkbench.repositories.git import GitClient

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9+/=_-]{8,}"
    ),
)

_SOURCE_KINDS = {
    "TASK": "tasks",
    "RUN": "runs",
    "EXPERIMENT": "experiments",
    "EXPERIMENT_COMPARISON": "comparisons",
    "ARTIFACT": "artifacts",
    "DOCUMENT": "documents",
    "CLAIM": "claims",
    "SNAPSHOT": "snapshots",
    "USER_NOTE": None,
}


class MemoryService:
    def __init__(
        self,
        *,
        store: Any,
        events: Any,
        projects: Any,
        tasks: Any = None,
        runs: Any = None,
        experiments: Any = None,
        artifacts: Any = None,
        documents: Any = None,
        claims: Any = None,
        snapshots: Any = None,
        repositories: Any = None,
    ):
        self.store = store
        self.events = events
        self.projects = projects
        self.tasks = tasks
        self.runs = runs
        self.experiments = experiments
        self.artifacts = artifacts
        self.documents = documents
        self.claims = claims
        self.snapshots = snapshots
        self.repositories = repositories

    # ------------------------------------------------------------------ write
    def add(
        self,
        *,
        project_id: str,
        kind: MemoryKind,
        title: str,
        statement: str,
        author_kind: MemoryAuthor = MemoryAuthor.USER,
        status: MemoryStatus | None = None,
        scope: str = "project",
        repository_id: str | None = None,
        snapshot_id: str | None = None,
        commit_sha: str | None = None,
        confidence: str = "MEDIUM",
        sources: list[MemorySource] | None = None,
        supersedes_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ProjectMemory:
        project = self.projects.get(project_id)
        if not title.strip() or not statement.strip():
            raise ProjectMemoryError("A memory needs a title and a statement")
        self._reject_secret(title, "title")
        self._reject_secret(statement, "statement")
        if author_kind == MemoryAuthor.MODEL and status in {
            MemoryStatus.CONFIRMED,
            MemoryStatus.STALE,
        }:
            raise ProjectMemoryError(
                "A model-authored memory cannot be created as confirmed",
                hint="Record it unconfirmed, then confirm it explicitly as the user.",
            )
        if status is None:
            status = (
                MemoryStatus.CONFIRMED
                if author_kind == MemoryAuthor.USER
                else MemoryStatus.UNCONFIRMED
            )
        if commit_sha and len(commit_sha) != 40:
            raise ProjectMemoryError("commit_sha must be a full 40-character SHA")
        if snapshot_id is not None and self.snapshots is not None:
            snapshot = self.snapshots.get(snapshot_id)
            if snapshot.repository_id != repository_id and repository_id is not None:
                raise ProjectMemoryError(
                    "The snapshot does not belong to the recorded repository"
                )
            repository_id = repository_id or snapshot.repository_id
        if repository_id is not None and self.repositories is not None:
            repository = self.repositories.get(repository_id)
            if repository.project_id != project.id:
                raise ProjectMemoryError("The repository belongs to a different project")

        validated_sources = [self._validate_source(item) for item in (sources or [])]
        memory = ProjectMemory(
            id="mem_" + new_ulid(),
            project_id=project.id,
            kind=kind,
            status=status,
            author_kind=author_kind,
            title=title.strip(),
            statement=statement.strip(),
            scope=scope.strip() or "project",
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            commit_sha=commit_sha,
            confidence=confidence,
            sources=validated_sources,
            supersedes_id=supersedes_id,
            created_at=utcnow(),
            updated_at=utcnow(),
            last_validated_at=utcnow()
            if status == MemoryStatus.CONFIRMED
            else None,
            details=details or {},
        )
        if supersedes_id:
            previous = self.store.get(supersedes_id)
            if previous.project_id != project.id:
                raise ProjectMemoryError("The superseded memory belongs to another project")
            self.store.set_status(
                previous.id,
                MemoryStatus.RETRACTED.value,
                note=f"superseded by {memory.id}",
            )
        self.store.create(memory)
        self._event(
            EventType.MEMORY_CREATED,
            memory,
            payload={
                "kind": memory.kind.value,
                "status": memory.status.value,
                "author": memory.author_kind.value,
                "title": memory.title,
                "sources": [
                    {"kind": item.kind, "ref": item.ref} for item in memory.sources
                ],
            },
        )
        return memory

    # ------------------------------------------------------------------- read
    def list(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ProjectMemory]:
        self.projects.get(project_id)
        return self.store.list(
            project_id=project_id, kind=kind, status=status, limit=limit
        )

    def get(self, memory_id: str) -> ProjectMemory:
        return self.store.get(memory_id)

    # ------------------------------------------------------------ experience
    def record_attempt(self, **kwargs: Any) -> ProjectMemory:
        """Record a failed approach as a scoped negative result (WP2-01)."""
        from mayaworkbench.memory.experience import record_attempt

        return record_attempt(self, **kwargs)

    def suggest_prior_attempts(
        self, *, project_id: str, goal: str, failure_signature: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        """Suggest prior attempts by similarity; suggestions never prohibit work."""
        from mayaworkbench.memory.experience import suggest_prior_attempts

        return suggest_prior_attempts(
            self,
            project_id=project_id,
            goal=goal,
            failure_signature=failure_signature,
            limit=limit,
        )

    # ---------------------------------------------------------------- changes
    def confirm(self, memory_id: str, *, note: str = "") -> ProjectMemory:
        memory = self.store.get(memory_id)
        if memory.status == MemoryStatus.RETRACTED:
            raise ProjectMemoryError(
                "A retracted memory cannot be confirmed",
                hint="Record a new memory that supersedes it.",
            )
        if memory.status == MemoryStatus.CONFIRMED:
            return memory
        self.store.set_status(memory_id, MemoryStatus.CONFIRMED.value, note=note)
        updated = self.store.get(memory_id)
        self._event(
            EventType.MEMORY_UPDATED,
            updated,
            payload={"status": updated.status.value, "note": note},
        )
        return updated

    def retract(self, memory_id: str, *, reason: str) -> ProjectMemory:
        if not reason.strip():
            raise ProjectMemoryError("A retraction needs a reason")
        memory = self.store.get(memory_id)
        self.store.set_status(memory_id, MemoryStatus.RETRACTED.value, note=reason)
        updated = self.store.get(memory_id)
        self._event(
            EventType.MEMORY_UPDATED,
            updated,
            payload={"status": updated.status.value, "reason": reason},
        )
        return updated

    def delete(self, memory_id: str) -> None:
        memory = self.store.get(memory_id)
        self.store.delete(memory_id)
        self._event(
            EventType.MEMORY_DELETED,
            memory,
            payload={"title": memory.title, "kind": memory.kind.value},
        )

    def mark_stale(self, *, project_id: str, repository_id: str) -> list[str]:
        """Mark confirmed commit-scoped memories for a moved repository."""
        repository = self.repositories.get(repository_id)
        if repository.project_id != project_id:
            raise ProjectMemoryError("The repository belongs to a different project")
        git = GitClient(Path(repository.root_path))
        current = git.head_commit()
        ids = self.store.mark_stale_by_commit(
            repository_id=repository_id, current_commit=current
        )
        for memory_id in ids:
            memory = self.store.get(memory_id)
            self._event(
                EventType.MEMORY_STALE,
                memory,
                payload={"current_commit": current},
            )
        return ids

    # ----------------------------------------------------------------- export
    def export(self, project_id: str, destination: str | Path) -> dict[str, str]:
        project = self.projects.get(project_id)
        memories = self.list(project_id, limit=10_000)
        target = Path(destination).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / "memories.json"
        md_path = target / "memories.md"
        payload = {
            "schema_version": "1.0",
            "project": project.model_dump(mode="json"),
            "generated_at": utcnow().isoformat(),
            "memories": [item.model_dump(mode="json") for item in memories],
        }
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        lines = [f"# Project memory: {project.name}", ""]
        if not memories:
            lines.append("No memory records exist.")
        for memory in memories:
            lines.extend(
                [
                    f"## {memory.title}",
                    "",
                    f"- id: `{memory.id}`",
                    f"- kind: `{memory.kind.value}`",
                    f"- status: `{memory.status.value}`",
                    f"- author: `{memory.author_kind.value}`",
                    f"- confidence: `{memory.confidence}`",
                    f"- scope: `{memory.scope}`",
                ]
            )
            if memory.commit_sha:
                lines.append(f"- commit: `{memory.commit_sha}`")
            lines.extend(["", memory.statement, ""])
            if memory.sources:
                lines.append("Sources:")
                for source in memory.sources:
                    lines.append(
                        f"- `{source.kind}` `{source.ref}`"
                        + (f" sha256 `{source.sha256}`" if source.sha256 else "")
                    )
                lines.append("")
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"json": str(json_path), "markdown": str(md_path)}

    # ---------------------------------------------------------------- private
    def _validate_source(self, source: MemorySource) -> MemorySource:
        if source.kind not in _SOURCE_KINDS:
            raise ProjectMemoryError(
                f"Unknown memory source kind: {source.kind}",
                hint=f"Use one of: {', '.join(sorted(_SOURCE_KINDS))}.",
            )
        lookup = _SOURCE_KINDS[source.kind]
        store = {
            "tasks": self.tasks,
            "runs": self.runs,
            "experiments": self.experiments,
            "comparisons": self.experiments,
            "artifacts": self.artifacts,
            "documents": self.documents,
            "claims": self.claims,
            "snapshots": self.snapshots,
        }.get(lookup or "")
        if store is None:
            return source
        try:
            if source.kind == "EXPERIMENT_COMPARISON":
                store.get_comparison(source.ref)
            elif source.kind == "EXPERIMENT":
                store.get_definition(source.ref)
            else:
                store.get(source.ref)
        except Exception as exc:  # noqa: BLE001 - normalise to a domain error
            raise ProjectMemoryError(
                f"Memory source {source.kind} {source.ref!r} does not exist"
            ) from exc
        return source

    def _reject_secret(self, value: str, field: str) -> None:
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise ProjectMemoryError(
                    f"The memory {field} looks like it contains a secret; refusing to store it",
                    hint="Describe the credential's location, never its value.",
                )

    def _event(self, event_type: EventType, memory: ProjectMemory, *, payload: dict[str, Any]) -> None:
        entity_type = "project_memory"
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=memory.project_id,
                repository_id=memory.repository_id,
                snapshot_id=memory.snapshot_id,
                entity_type=entity_type,
                entity_id=memory.id,
                actor_kind=ActorKind.USER,
                actor_name="memory",
                payload=payload,
            )
        )


__all__ = ["MemoryService"]
