"""Composition root.

One object graph is built per process. The CLI, and later the HTTP API, call the
same services; neither reimplements business logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mayaworkbench.clock import utcnow
from mayaworkbench.config import AppPaths, Settings, load_settings, save_settings
from mayaworkbench.domain.enums import ActorKind, EventType
from mayaworkbench.domain.models import Project, WorkflowEvent
from mayaworkbench.errors import ConflictError, UsageError
from mayaworkbench.ids import new_ulid
from mayaworkbench.experiments.service import ExperimentService
from mayaworkbench.indexing.indexer import Indexer
from mayaworkbench.indexing.map import RepositoryMapService
from mayaworkbench.memory.service import MemoryService
from mayaworkbench.mapping.service import ClaimMappingService
from mayaworkbench.tasks.approvals import ApprovalService
from mayaworkbench.tasks.plans import PlanService
from mayaworkbench.tasks.review import ReviewService
from mayaworkbench.tasks.service import TaskService
from mayaworkbench.tasks.workflow import WorkflowService
from mayaworkbench.verification.service import VerificationService
from mayaworkbench.workspace.worktree import WorktreeManager
from mayaworkbench.models.gateway import ModelGateway, ProviderFactory
from mayaworkbench.qa.service import AskService
from mayaworkbench.repositories.snapshots import RepositoryService
from mayaworkbench.research.claims import ClaimService
from mayaworkbench.research.ingestion import DocumentIngestionService
from mayaworkbench.research.memo import MemoService
from mayaworkbench.retrieval.context import ContextPacker
from mayaworkbench.retrieval.search import SearchService
from mayaworkbench.retrieval.service import RetrievalService
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.db import Database
from mayaworkbench.storage.stores import (
    ApprovalStore,
    ClaimMappingStore,
    ContractStore,
    DiffStore,
    EventStore,
    PlanStore,
    ReviewStore,
    RunStore,
    TaskStore,
    ToolCallStore,
    VerificationStore,
    WorkerJobStore,
    WorkspaceStore,
    ExperimentStore,
    MemoryStore,
    FileStore,
    InvocationStore,
    IssueStore,
    ProjectStore,
    QaStore,
    RepositoryStore,
    ResearchClaimStore,
    ResearchDocumentStore,
    ResearchExtractionStore,
    ResearchMemoStore,
    SnapshotStore,
    SourcePassageStore,
    SymbolStore,
)


def slugify(name: str, *, fallback: str = "project") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:62].strip("-")
    return slug or fallback


@dataclass
class Services:
    paths: AppPaths
    settings: Settings
    db: Database
    artifacts: ArtifactStore
    projects: ProjectStore
    repositories: RepositoryStore
    snapshots: SnapshotStore
    files: FileStore
    symbols: SymbolStore
    issues: IssueStore
    events: EventStore
    invocations: InvocationStore
    qa_store: QaStore
    repository_service: RepositoryService
    indexer: Indexer
    search: SearchService
    packer: ContextPacker
    retrieval: RetrievalService
    gateway: ModelGateway
    ask: AskService
    maps: RepositoryMapService
    documents: ResearchDocumentStore
    passages: SourcePassageStore
    claims: ResearchClaimStore
    mappings: ClaimMappingStore
    memos: ResearchMemoStore
    extractions: ResearchExtractionStore
    ingestion: DocumentIngestionService
    claim_service: ClaimService
    mapping_service: ClaimMappingService
    memo_service: MemoService
    task_store: TaskStore
    contracts: ContractStore
    task_plans: PlanStore
    workflow_runs: RunStore
    task_approvals: ApprovalStore
    execution_workspaces: WorkspaceStore
    tool_calls: ToolCallStore
    task_diffs: DiffStore
    task_reviews: ReviewStore
    jobs: WorkerJobStore
    plan_service: PlanService
    approval_service: ApprovalService
    workflow: WorkflowService
    review_service: ReviewService
    worktrees: WorktreeManager
    verification_store: VerificationStore
    verification_service: VerificationService
    experiment_store: ExperimentStore
    experiment_service: ExperimentService
    memory_store: MemoryStore
    memory_service: MemoryService
    task_service: TaskService

    @classmethod
    def open(
        cls,
        home: str | None = None,
        *,
        migrate: bool = True,
        provider_factory: ProviderFactory | None = None,
    ) -> "Services":
        paths = AppPaths.resolve(home)
        paths.ensure_dirs()
        settings = load_settings(paths)
        if not paths.config_path.exists():
            save_settings(paths, settings)
        db = Database(paths.db_path)
        if migrate:
            db.migrate()
        artifacts = ArtifactStore(db, paths.artifacts_dir)
        projects = ProjectStore(db)
        repositories = RepositoryStore(db)
        snapshots = SnapshotStore(db)
        files = FileStore(db)
        symbols = SymbolStore(db)
        issues = IssueStore(db)
        events = EventStore(db)
        invocations = InvocationStore(db)
        qa_store = QaStore(db)

        repository_service = RepositoryService(
            db, projects, repositories, snapshots, artifacts, settings, events
        )
        indexer = Indexer(
            db,
            files=files,
            symbols=symbols,
            issues=issues,
            snapshots=snapshots,
            repositories=repositories,
            events=events,
            artifacts=artifacts,
            repository_service=repository_service,
            settings=settings,
        )
        search = SearchService(db, files, symbols)
        packer = ContextPacker(files, settings.retrieval)
        retrieval = RetrievalService(search, packer, settings.retrieval)
        gateway = ModelGateway(settings, invocations, events, provider_factory=provider_factory)
        ask = AskService(
            db,
            projects=projects,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            qa_store=qa_store,
            artifacts=artifacts,
            events=events,
            retrieval=retrieval,
            gateway=gateway,
            settings=settings,
        )
        maps = RepositoryMapService(
            db,
            snapshots=snapshots,
            repositories=repositories,
            files=files,
            symbols=symbols,
            issues=issues,
        )
        documents = ResearchDocumentStore(db)
        passages = SourcePassageStore(db)
        claims = ResearchClaimStore(db)
        mappings = ClaimMappingStore(db)
        memos = ResearchMemoStore(db)
        extractions = ResearchExtractionStore(db)
        ingestion = DocumentIngestionService(
            db,
            projects=projects,
            documents=documents,
            passages=passages,
            claims=claims,
            artifacts=artifacts,
            events=events,
            settings=settings,
        )
        claim_service = ClaimService(
            db,
            projects=projects,
            documents=documents,
            passages=passages,
            claims=claims,
            extractions=extractions,
            artifacts=artifacts,
            events=events,
            gateway=gateway,
            settings=settings,
        )
        mapping_service = ClaimMappingService(
            db,
            projects=projects,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            symbols=symbols,
            claims=claims,
            mappings=mappings,
            search=search,
            gateway=gateway,
            artifacts=artifacts,
            events=events,
            settings=settings,
        )
        memo_service = MemoService(
            db,
            projects=projects,
            documents=documents,
            passages=passages,
            claims=claims,
            memos=memos,
            snapshots=snapshots,
            files=files,
            issues=issues,
            mappings=mappings,
            mapping_service=mapping_service,
            artifacts=artifacts,
            events=events,
            settings=settings,
        )

        task_store = TaskStore(db)
        contracts = ContractStore(db)
        task_plans = PlanStore(db)
        workflow_runs = RunStore(db)
        task_approvals = ApprovalStore(db)
        execution_workspaces = WorkspaceStore(db)
        tool_calls = ToolCallStore(db)
        task_diffs = DiffStore(db)
        task_reviews = ReviewStore(db)
        jobs = WorkerJobStore(db)
        verification_store = VerificationStore(db)
        experiment_store = ExperimentStore(db)
        memory_store = MemoryStore(db)
        memory_service = MemoryService(
            store=memory_store,
            events=events,
            projects=projects,
            tasks=task_store,
            runs=workflow_runs,
            experiments=experiment_store,
            artifacts=artifacts,
            documents=documents,
            claims=claims,
            snapshots=snapshots,
            repositories=repositories,
        )
        experiment_service = ExperimentService(
            home=paths.home,
            settings=settings,
            artifacts=artifacts,
            events=events,
            store=experiment_store,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            tasks=task_store,
            contracts=contracts,
            memory=memory_service,
        )
        plan_service = PlanService(
            settings=settings,
            gateway=gateway,
            plans=task_plans,
            runs=workflow_runs,
            files=files,
            artifacts=artifacts,
            events=events,
            claims=claims,
        )
        workflow = WorkflowService(workflow_runs, events)
        approval_service = ApprovalService(
            task_approvals, workflow_runs, task_plans, events
        )
        review_service = ReviewService(
            diffs=task_diffs, reviews=task_reviews, artifacts=artifacts, events=events
        )
        worktrees = WorktreeManager(
            home=paths.home,
            db=db,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            artifacts=artifacts,
            workspaces=execution_workspaces,
            events=events,
        )
        verification_service = VerificationService(
            home=paths.home,
            db=db,
            settings=settings,
            artifacts=artifacts,
            events=events,
            runs=workflow_runs,
            tasks=task_store,
            contracts=contracts,
            plans=task_plans,
            diffs=task_diffs,
            reviews=task_reviews,
            review_service=review_service,
            workspaces=execution_workspaces,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            approvals=task_approvals,
            invocations=invocations,
            store=verification_store,
            gateway=gateway,
            experiments=experiment_service,
        )
        task_service = TaskService(
            paths=paths,
            settings=settings,
            db=db,
            projects=projects,
            repositories=repositories,
            snapshots=snapshots,
            repository_service=repository_service,
            tasks=task_store,
            contracts=contracts,
            plans=task_plans,
            runs=workflow_runs,
            approvals=task_approvals,
            workspaces=execution_workspaces,
            calls=tool_calls,
            diffs=task_diffs,
            reviews=task_reviews,
            jobs=jobs,
            artifacts=artifacts,
            events=events,
            gateway=gateway,
            plan_service=plan_service,
            approval_service=approval_service,
            workflow=workflow,
            review_service=review_service,
            worktrees=worktrees,
            documents=documents,
            claims=claims,
            verification=verification_service,
        )
        return cls(
            paths=paths,
            settings=settings,
            db=db,
            artifacts=artifacts,
            projects=projects,
            repositories=repositories,
            snapshots=snapshots,
            files=files,
            symbols=symbols,
            issues=issues,
            events=events,
            invocations=invocations,
            qa_store=qa_store,
            repository_service=repository_service,
            indexer=indexer,
            search=search,
            packer=packer,
            retrieval=retrieval,
            gateway=gateway,
            ask=ask,
            maps=maps,
            documents=documents,
            passages=passages,
            claims=claims,
            mappings=mappings,
            memos=memos,
            extractions=extractions,
            ingestion=ingestion,
            claim_service=claim_service,
            mapping_service=mapping_service,
            memo_service=memo_service,
            task_store=task_store,
            contracts=contracts,
            task_plans=task_plans,
            workflow_runs=workflow_runs,
            task_approvals=task_approvals,
            execution_workspaces=execution_workspaces,
            tool_calls=tool_calls,
            task_diffs=task_diffs,
            task_reviews=task_reviews,
            jobs=jobs,
            plan_service=plan_service,
            approval_service=approval_service,
            workflow=workflow,
            review_service=review_service,
            worktrees=worktrees,
            verification_store=verification_store,
            verification_service=verification_service,
            experiment_store=experiment_store,
            experiment_service=experiment_service,
            memory_store=memory_store,
            memory_service=memory_service,
            task_service=task_service,
        )

    # Short aliases used by the worker/agent layer; the dataclass fields keep
    # explicit names so the storage boundary stays obvious.
    @property
    def tasks(self) -> TaskStore:
        return self.task_store

    @property
    def plans(self) -> PlanStore:
        return self.task_plans

    @property
    def runs(self) -> RunStore:
        return self.workflow_runs

    @property
    def approvals(self) -> ApprovalStore:
        return self.task_approvals

    @property
    def workspaces(self) -> WorkspaceStore:
        return self.execution_workspaces

    @property
    def calls(self) -> ToolCallStore:
        return self.tool_calls

    @property
    def diffs(self) -> DiffStore:
        return self.task_diffs

    @property
    def reviews(self) -> ReviewStore:
        return self.task_reviews

    @property
    def verification(self) -> VerificationService:
        return self.verification_service

    @property
    def experiments(self) -> ExperimentService:
        return self.experiment_service

    @property
    def memory(self) -> MemoryService:
        return self.memory_service

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- projects
    def create_project(self, name: str, description: str = "", slug: str | None = None) -> Project:
        chosen = (slug or slugify(name)).strip()
        if self.projects.find(chosen) is not None:
            raise ConflictError(
                f"A project with slug {chosen!r} already exists",
                hint="Choose a different name or pass --slug.",
            )
        project = Project(
            id="proj_" + new_ulid(),
            name=name.strip(),
            slug=chosen,
            description=description.strip(),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        with self.db.transaction():
            self.projects.create(project)
            self.events.append(
                WorkflowEvent(
                    event_id="evt_" + new_ulid(),
                    event_type=EventType.PROJECT_CREATED,
                    project_id=project.id,
                    entity_type="project",
                    entity_id=project.id,
                    actor_kind=ActorKind.USER,
                    actor_name="local-user",
                    payload={"name": project.name, "slug": project.slug},
                )
            )
        return project

    def default_project(self) -> Project:
        projects = self.projects.list()
        if len(projects) == 1:
            return projects[0]
        if not projects:
            raise UsageError(
                "No project exists yet",
                hint="Run `maya init` or `maya project create <name>` first.",
            )
        raise UsageError(
            "Several projects exist; pass --project <slug>",
            hint="Available: " + ", ".join(project.slug for project in projects),
        )

    def resolve_project(self, identifier: str | None) -> Project:
        if identifier:
            return self.projects.get(identifier)
        return self.default_project()

    def resolve_snapshot_context(
        self,
        *,
        project_identifier: str | None = None,
        repository_identifier: str | None = None,
        snapshot_id: str | None = None,
        require_indexed: bool = True,
    ):
        """Resolve ``(project, repository, snapshot)`` for one command.

        An explicit snapshot already identifies its repository, so it must not
        force the caller to disambiguate a multi-repository project again.
        """
        project = self.resolve_project(project_identifier)
        if snapshot_id:
            snapshot = self.snapshots.get(snapshot_id)
            repository = self.repositories.get(snapshot.repository_id)
            if repository.project_id != project.id:
                owner = self.projects.get(repository.project_id)
                raise UsageError(
                    f"Snapshot {snapshot_id} belongs to project {owner.slug!r}",
                    hint=f"Pass --project {owner.slug} or omit --project.",
                )
            if repository_identifier:
                requested = self.repositories.get(repository_identifier)
                if requested.id != repository.id:
                    raise UsageError(
                        f"Snapshot {snapshot_id} does not belong to repository {repository_identifier}"
                    )
            if require_indexed and snapshot.indexed_at is None:
                raise UsageError(
                    f"Snapshot {snapshot_id} has not been indexed",
                    hint=f"Run `maya index --snapshot {snapshot_id}`.",
                )
            return project, repository, snapshot
        repository = self.resolve_repository(project, repository_identifier)
        snapshot = self.resolve_snapshot(
            project,
            snapshot_id=None,
            repository_id=repository.id,
            require_indexed=require_indexed,
        )
        return project, repository, snapshot

    def resolve_repository(self, project: Project, identifier: str | None):  # type: ignore[no-untyped-def]
        if identifier:
            repository = self.repositories.get(identifier)
            if repository.project_id != project.id:
                raise UsageError(
                    f"Repository {identifier} belongs to a different project",
                    hint="Pass --project for the repository's project.",
                )
            return repository
        repositories = self.repositories.list_by_project(project.id)
        if not repositories:
            raise UsageError(
                f"Project {project.slug!r} has no repository attached",
                hint="Run `maya repo add <path> --project <slug>`.",
            )
        if len(repositories) > 1:
            raise UsageError(
                "Several repositories are attached; pass --repo <id>",
                hint="Available: " + ", ".join(repo.id for repo in repositories),
            )
        return repositories[0]

    def resolve_snapshot(
        self,
        project: Project,
        *,
        snapshot_id: str | None = None,
        repository_id: str | None = None,
        require_indexed: bool = True,
    ):
        if snapshot_id:
            snapshot = self.snapshots.get(snapshot_id)
            if require_indexed and snapshot.indexed_at is None:
                raise UsageError(
                    f"Snapshot {snapshot_id} has not been indexed",
                    hint=f"Run `maya index --snapshot {snapshot_id}`.",
                )
            return snapshot
        candidates = (
            self.snapshots.list_by_repository(repository_id, limit=25)
            if repository_id
            else self.snapshots.list_for_project(project.id, limit=25)
        )
        if not candidates:
            raise UsageError(
                "No snapshot exists yet",
                hint="Run `maya snapshot create` to pin a revision.",
            )
        if require_indexed:
            indexed = [snapshot for snapshot in candidates if snapshot.indexed_at is not None]
            if not indexed:
                raise UsageError(
                    "No indexed snapshot exists yet",
                    hint="Run `maya index` to build the index.",
                )
            return indexed[0]
        return candidates[0]


def home_dir_from_args(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


__all__ = ["Services", "slugify", "home_dir_from_args"]
