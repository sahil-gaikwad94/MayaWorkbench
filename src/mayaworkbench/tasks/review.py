"""Deterministic review of the candidate patch.

This is Phase 3's diff review, not Phase 4's verification engine. It checks
structural properties that can be established without running anything:

* scope violations and excluded paths;
* credential-shaped paths and content introduced by the patch;
* test file modification/deletion and added skip/xfail markers;
* dependency and binary changes;
* size and no-op warnings.

Findings are explicit codes with severity. The review never says a change is
correct, and a clean review does not mean the tests pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import (
    ActorKind,
    ArtifactKind,
    EventType,
)
from mayaworkbench.domain.models import (
    ReviewFinding,
    Task,
    TaskContract,
    TaskDiff,
    TaskReview,
    WorkflowEvent,
    WorkflowRun,
)
from mayaworkbench.errors import TaskError
from mayaworkbench.ids import new_ulid
from mayaworkbench.storage.artifacts import ArtifactStore
from mayaworkbench.storage.stores import (
    DiffStore,
    EventStore,
    ReviewStore,
)
from mayaworkbench.workspace.policy import (
    PathPolicy,
    contains_secret_material,
    is_secret_path,
)

TEST_SKIP_RE = re.compile(
    r"^\+\s*(?:@pytest\.mark\.(?:skip|xfail)|@unittest\.skip|pytest\.skip\(|"
    r"self\.skipTest\(|@skip\b)"
)
DEPENDENCY_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
}
DEPENDENCY_PATTERNS = (
    "requirements/*.txt",
    "requirements*.txt",
    "requirements/*.in",
)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{path}"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


class ReviewService:
    def __init__(
        self,
        *,
        diffs: DiffStore,
        reviews: ReviewStore,
        artifacts: ArtifactStore,
        events: EventStore,
    ):
        self.diffs = diffs
        self.reviews = reviews
        self.artifacts = artifacts
        self.events = events

    def review(
        self,
        *,
        run: WorkflowRun,
        task: Task,
        contract: TaskContract,
        diff: TaskDiff,
    ) -> TaskReview:
        existing = self.reviews.get_by_run(run.id)
        if existing is not None:
            return existing
        patch = self.artifacts.read_text(diff.patch_artifact_id)
        policy = PathPolicy(
            Path("."),
            allowed_paths=contract.scope.allowed_paths,
            excluded_paths=contract.scope.excluded_paths,
            allow_file_deletion=contract.constraints.allow_file_deletion,
        )
        findings: list[ReviewFinding] = []
        tests_modified: list[str] = []
        dependency_changes: list[str] = []

        if not diff.original_checkout_unchanged:
            findings.append(
                ReviewFinding(
                    severity="ERROR",
                    code="ORIGINAL_CHECKOUT_CHANGED",
                    message=(
                        "The original checkout's HEAD or status changed during the run; "
                        "the worktree isolation was not sufficient and the user must "
                        "inspect their checkout before accepting anything."
                    ),
                )
            )
        if diff.scope_violations:
            for path in diff.scope_violations:
                findings.append(
                    ReviewFinding(
                        severity="ERROR",
                        code="SCOPE_VIOLATION",
                        message=f"Changed path is outside the approved scope: {path}",
                        path=path,
                    )
                )
        if not diff.files:
            findings.append(
                ReviewFinding(
                    severity="WARNING",
                    code="NO_CHANGES",
                    message="The candidate patch is empty; no implementation was recorded.",
                )
            )
        for file in diff.files:
            if is_secret_path(file.path):
                findings.append(
                    ReviewFinding(
                        severity="ERROR",
                        code="SECRET_PATH",
                        message="Patch touches a credential-shaped path.",
                        path=file.path,
                    )
                )
            if policy.is_excluded(file.path):
                findings.append(
                    ReviewFinding(
                        severity="ERROR",
                        code="EXCLUDED_PATH",
                        message="Patch touches a path excluded by the contract.",
                        path=file.path,
                    )
                )
            if file.binary:
                findings.append(
                    ReviewFinding(
                        severity="WARNING",
                        code="BINARY_CHANGE",
                        message="Binary change cannot be reviewed as text.",
                        path=file.path,
                    )
                )
            if is_test_path(file.path):
                tests_modified.append(file.path)
                findings.append(
                    ReviewFinding(
                        severity="WARNING" if file.status != "deleted" else "ERROR",
                        code="TEST_FILE_DELETED" if file.status == "deleted" else "TEST_FILE_MODIFIED",
                        message=(
                            "A test file was deleted."
                            if file.status == "deleted"
                            else "A test file was modified; inspect for weakened assertions."
                        ),
                        path=file.path,
                    )
                )
            if file.path.rsplit("/", 1)[-1] in DEPENDENCY_FILES or any(
                file.path.startswith(prefix.rstrip("*")) for prefix in DEPENDENCY_PATTERNS
            ):
                dependency_changes.append(file.path)
                findings.append(
                    ReviewFinding(
                        severity="WARNING",
                        code="DEPENDENCY_CHANGE",
                        message="A dependency or packaging file changed; justify it explicitly.",
                        path=file.path,
                    )
                )
        secret_content = contains_secret_material(patch)
        if secret_content:
            findings.append(
                ReviewFinding(
                    severity="ERROR",
                    code="SECRET_CONTENT",
                    message="The patch contains credential-shaped material.",
                )
            )
        skip_markers = [
            line for line in patch.splitlines() if TEST_SKIP_RE.match(line)
        ]
        if skip_markers:
            findings.append(
                ReviewFinding(
                    severity="WARNING",
                    code="POTENTIAL_TEST_WEAKENING",
                    message=(
                        f"The patch adds {len(skip_markers)} skip/xfail marker(s); "
                        "these are warnings, not proof of weakening."
                    ),
                )
            )
        if diff.additions + diff.deletions > 5000:
            findings.append(
                ReviewFinding(
                    severity="WARNING",
                    code="LARGE_DIFF",
                    message=(
                        f"Patch changes {diff.additions + diff.deletions} lines; "
                        "reviewability is reduced."
                    ),
                )
            )
        if diff.warnings:
            for warning in diff.warnings:
                findings.append(
                    ReviewFinding(severity="INFO", code="DIFF_WARNING", message=warning)
                )

        review = TaskReview(
            id="rev_" + new_ulid(),
            run_id=run.id,
            task_id=task.id,
            project_id=run.project_id,
            diff_id=diff.id,
            scope_ok=not any(
                finding.severity == "ERROR"
                and finding.code
                in {
                    "SCOPE_VIOLATION",
                    "EXCLUDED_PATH",
                    "SECRET_PATH",
                    "ORIGINAL_CHECKOUT_CHANGED",
                }
                for finding in findings
            ),
            secrets_found=secret_content
            or any(finding.code == "SECRET_PATH" for finding in findings),
            tests_modified=tests_modified,
            dependency_changes=dependency_changes,
            findings=findings,
        )
        report = {
            "run_id": run.id,
            "task_id": task.id,
            "diff_id": diff.id,
            "patch_sha256": diff.patch_sha256,
            "base_commit_sha": diff.base_commit_sha,
            "files": [item.model_dump(mode="json") for item in diff.files],
            "additions": diff.additions,
            "deletions": diff.deletions,
            "scope_ok": review.scope_ok,
            "secrets_found": review.secrets_found,
            "original_checkout_unchanged": diff.original_checkout_unchanged,
            "tests_modified": tests_modified,
            "dependency_changes": dependency_changes,
            "findings": [item.model_dump(mode="json") for item in findings],
            "note": (
                "Structural review only. Functional verification and acceptance "
                "are separate (`maya verify run`); a clean review is not a test result."
            ),
            "reviewed_at": utcnow().isoformat(),
        }
        artifact = self.artifacts.put_text(
            json.dumps(report, indent=2, sort_keys=True),
            kind=ArtifactKind.DIFF_REVIEW,
            media_type="application/json",
            created_by="diff_review",
        )
        review.report_artifact_id = artifact.id
        self.reviews.create(review)
        self.events.append(
            WorkflowEvent(
                event_id="evt_" + new_ulid(),
                event_type=EventType.DIFF_REVIEWED,
                project_id=run.project_id,
                snapshot_id=run.snapshot_id,
                task_id=run.task_id,
                run_id=run.id,
                entity_type="review",
                entity_id=review.id,
                actor_kind=ActorKind.SYSTEM,
                actor_name="diff_review",
                payload={
                    "diff_id": diff.id,
                    "scope_ok": review.scope_ok,
                    "secrets_found": review.secrets_found,
                    "findings": len(findings),
                    "errors": sum(1 for item in findings if item.severity == "ERROR"),
                },
            )
        )
        return review


__all__ = ["ReviewService", "is_test_path"]
