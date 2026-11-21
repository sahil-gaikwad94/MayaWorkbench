"""Engineering Workspace application service (WP4).

A Workspace is the durable scope for a research/engineering effort. It may
attach several repositories and non-code resources, describe environments, and
accumulate evidence. The existing Project/Repository/Snapshot records remain
the source of truth for pinned code revisions; a workspace references them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mayaworkbench.domain.enums import (
    ActorKind,
    EvidenceStatus,
    EventType,
    ResourceKind,
    WorkspaceSessionState,
)
from mayaworkbench.domain.models import (
    EnvironmentSpec,
    EvidenceItem,
    ResourceAttachment,
    WorkflowEvent,
    Workspace,
    WorkspaceSession,
)
from mayaworkbench.errors import ConflictError, NotFoundError, UsageError
from mayaworkbench.ids import new_ulid
from mayaworkbench.storage.stores import (
    EngineeringWorkspaceStore,
    EventStore,
    ProjectStore,
    RepositoryStore,
)


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class WorkspaceService:
    def __init__(
        self,
        *,
        store: EngineeringWorkspaceStore,
        events: EventStore,
        projects: ProjectStore,
        repositories: RepositoryStore,
    ):
        self.store = store
        self.events = events
        self.projects = projects
        self.repositories = repositories

    # -------------------------------------------------------------- workspace
    def create(
        self,
        project_id: str,
        slug: str,
        *,
        title: str = "",
        description: dict[str, Any] | None = None,
        created_by: str = "USER",
    ) -> Workspace:
        self.projects.get(project_id)
        workspace = Workspace(
            id="ws_" + new_ulid(),
            project_id=project_id,
            slug=slug,
            title=title,
            description=description or {},
            created_by=created_by,
        )
        self.store.create_workspace(workspace)
        self._event(
            EventType.WORKSPACE_CREATED,
            project_id,
            workspace.id,
            {"slug": slug, "title": title},
        )
        return workspace

    def get(self, workspace_id: str) -> Workspace:
        return self.store.get_workspace(workspace_id)

    def list(self, project_id: str | None = None) -> list[Workspace]:
        return self.store.list_workspaces(project_id)

    def graph(self, workspace_id: str) -> dict:
        self.get(workspace_id)
        return self.store.graph(workspace_id)

    # --------------------------------------------------------------- resource
    def attach_resource(
        self,
        workspace_id: str,
        kind: str,
        name: str,
        ref: str,
        *,
        revision: str | None = None,
        hash_sha256: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ResourceAttachment:
        workspace = self.get(workspace_id)
        try:
            resource_kind = ResourceKind(kind)
        except ValueError as exc:
            raise UsageError(
                f"unknown resource kind {kind!r}",
                hint="Choose from " + ", ".join(item.value for item in ResourceKind),
            ) from exc
        if resource_kind is ResourceKind.REPOSITORY:
            # Attach by repository id so a workspace never holds a raw path it
            # has not verified. Unknown ids fail before any row is written.
            self.repositories.get(ref)
        attachment = ResourceAttachment(
            id="res_" + new_ulid(),
            workspace_id=workspace.id,
            kind=resource_kind,
            name=name,
            ref=ref,
            revision=revision,
            hash_sha256=hash_sha256,
            details=details or {},
        )
        self.store.attach_resource(attachment)
        self._event(
            EventType.RESOURCE_ATTACHED,
            workspace.project_id,
            workspace.id,
            {"kind": resource_kind.value, "name": name, "ref": ref},
        )
        return attachment

    # ------------------------------------------------------------ environment
    def add_environment(
        self,
        workspace_id: str,
        name: str,
        ecosystem: str,
        *,
        descriptor_path: str | None = None,
        lockfile_path: str | None = None,
        runtime: str = "LOCAL_TRUSTED",
        toolchain: dict[str, Any] | None = None,
    ) -> EnvironmentSpec:
        workspace = self.get(workspace_id)
        spec = EnvironmentSpec(
            id="env_" + new_ulid(),
            workspace_id=workspace.id,
            name=name,
            ecosystem=ecosystem,
            descriptor_path=descriptor_path,
            lockfile_path=lockfile_path,
            runtime=runtime,
            toolchain=toolchain or {},
            hash_sha256=_sha256(
                {
                    "name": name,
                    "ecosystem": ecosystem,
                    "descriptor": descriptor_path,
                    "lockfile": lockfile_path,
                    "runtime": runtime,
                    "toolchain": toolchain or {},
                }
            ),
        )
        self.store.add_environment(spec)
        self._event(
            EventType.ENVIRONMENT_SPECIFIED,
            workspace.project_id,
            workspace.id,
            {"name": name, "ecosystem": ecosystem, "runtime": runtime},
        )
        return spec

    # ---------------------------------------------------------------- session
    def record_session(
        self,
        workspace_id: str,
        command: list[str],
        *,
        runtime_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> WorkspaceSession:
        workspace = self.get(workspace_id)
        session = WorkspaceSession(
            id="wses_" + new_ulid(),
            workspace_id=workspace.id,
            runtime_id=runtime_id,
            command=command,
            details=details or {},
        )
        self.store.create_session(session)
        self._event(
            EventType.WORKSPACE_SESSION_OPENED,
            workspace.project_id,
            workspace.id,
            {"session_id": session.id, "command": command[:8]},
        )
        return session

    def close_session(
        self, workspace_id: str, session_id: str, *, reason: str = "closed"
    ) -> WorkspaceSession:
        workspace = self.get(workspace_id)
        sessions = {item.id: item for item in self.store.sessions(workspace_id)}
        session = sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"Workspace session not found: {session_id}")
        if session.state is not WorkspaceSessionState.OPEN:
            raise ConflictError(f"Session {session_id} is already {session.state.value}")
        closed = session.model_copy(
            update={
                "state": WorkspaceSessionState.STOPPED,
                "details": {**session.details, "close_reason": reason},
            }
        )
        # A client-side record update is expressed through a new row with the
        # same id; the store keeps the latest state for this workspace.
        self.store.close_session(closed)
        self._event(
            EventType.WORKSPACE_SESSION_CLOSED,
            workspace.project_id,
            workspace.id,
            {"session_id": session_id, "reason": reason},
        )
        return closed

    # --------------------------------------------------------------- evidence
    def attach_evidence(
        self,
        workspace_id: str,
        claim: str,
        *,
        resource_id: str | None = None,
        artifact_id: str | None = None,
        hash_sha256: str | None = None,
        status: str = EvidenceStatus.UNVERIFIED.value,
    ) -> EvidenceItem:
        workspace = self.get(workspace_id)
        try:
            evidence_status = EvidenceStatus(status)
        except ValueError as exc:
            raise UsageError(f"unknown evidence status {status!r}") from exc
        item = EvidenceItem(
            id="evi_" + new_ulid(),
            workspace_id=workspace.id,
            resource_id=resource_id,
            claim=claim,
            artifact_id=artifact_id,
            hash_sha256=hash_sha256 or _sha256({"claim": claim, "resource": resource_id}),
            status=evidence_status,
        )
        self.store.add_evidence(item)
        self._event(
            EventType.EVIDENCE_ATTACHED,
            workspace.project_id,
            workspace.id,
            {"claim": claim[:200], "status": evidence_status.value},
        )
        return item

    def _event(
        self, event_type: EventType, project_id: str, workspace_id: str, payload: dict[str, Any]
    ) -> None:
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=event_type,
                project_id=project_id,
                entity_type="engineering_workspace",
                entity_id=workspace_id,
                actor_kind=ActorKind.USER,
                actor_name="workspace",
                payload=payload,
            )
        )


__all__ = ["WorkspaceService"]
