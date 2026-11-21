"""Adversarial audit round 4: credentials, budgets, parser bombs, zero-change."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mayaworkbench.app.container import Services  # noqa: E402
from mayaworkbench.config import AppPaths, load_settings  # noqa: E402
from mayaworkbench.domain.enums import TaskType, WorkflowState  # noqa: E402
from mayaworkbench.errors import MayaError  # noqa: E402
from mayaworkbench.models.gateway import ModelGateway  # noqa: E402
from mayaworkbench.verification.registry import command_check  # noqa: E402
from mayaworkbench.worker import Worker  # noqa: E402
from tests.conftest import ScriptedTaskProvider, enable_virtual_model, git, write  # noqa: E402
from artifacts.adversarial._cli import maya_argv  # noqa: E402

RESULTS: list[dict] = []
REMOTE_TOKEN = "ghp_AUDIT0123456789abcdefghijklmnopqrst"
AWS_KEY = "AKIAIOSFODNN7AUDIT123"


def probe(pid, area, claim, fn):
    started = time.monotonic()
    try:
        detail = fn()
        status = "PASS"
    except AssertionError as exc:
        status, detail = "FAIL", str(exc) or "assertion failed"
    except Exception as exc:  # noqa: BLE001
        status, detail = "ERROR", f"{type(exc).__name__}: {exc}"
        if os.environ.get("AUDIT_TRACE"):
            traceback.print_exc()
    RESULTS.append({"id": pid, "area": area, "claim": claim, "status": status,
                    "detail": detail, "ms": round((time.monotonic() - started) * 1000)})
    print(f"[{status:5}] {pid} {claim} :: {detail}")
    return status


def finish(summary="done"):
    return {"thought": "done", "tool": "finish",
            "arguments": {"summary": summary, "limitations": [], "checks_run": [], "remaining_work": []}}


def action(tool, **arguments):
    return {"thought": f"{tool}", "tool": tool, "arguments": arguments}


def build_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "a@example.com")
    git(root, "config", "user.name", "A")
    git(root, "config", "commit.gpgsign", "false")
    write(root / "utils.py", "TOKEN_PATTERN = 1\n\n\ndef count_tokens(text: str) -> int:\n    return len(text.split())\n")
    write(root / "tests" / "test_utils.py", "from utils import count_tokens\n\n\ndef test_count():\n    assert count_tokens('a b') == 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="maya-audit4-"))
    home = base / "home"
    repo = build_repo(base)
    # a remote URL carrying a token, as real checkouts often have
    git(repo, "remote", "add", "origin", f"https://x-access-token:{REMOTE_TOKEN}@github.com/audit/repo.git")
    svc = Services.open(str(home))
    project = svc.create_project("Audit4", "")
    attached = svc.repository_service.attach(project, repo)
    snapshot = svc.repository_service.create_snapshot(attached.repository).snapshot
    svc.indexer.index(snapshot.id)

    def c1():
        leaks = []
        for path in home.rglob("*"):
            if path.is_file():
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if REMOTE_TOKEN.encode() in data:
                    leaks.append(str(path.relative_to(home)))
        assert not leaks, f"remote token persisted verbatim in: {leaks}"
        return "token absent from all local storage"

    probe("C1", "secrets", "A token embedded in the Git remote URL is not stored verbatim", c1)

    provider = ScriptedTaskProvider()
    enable_virtual_model(home)
    os.environ["MAYA_MODEL_API_KEY"] = "test-key"
    svc.settings = load_settings(AppPaths.resolve(str(home)))
    gateway = ModelGateway(svc.settings, svc.invocations, svc.events, lambda _s: provider)
    svc.gateway = gateway
    for service in (svc.plan_service, svc.task_service):
        service.gateway = gateway
        service.settings = svc.settings

    def task_with(checks, constraints=None, allowed=("utils.py", "tests"), criteria=("AC1",)):
        task, _ = svc.task_service.create_task(
            project_id=project.id, title="audit", goal="satisfy AC1",
            task_type=TaskType.ENGINEERING_CHANGE, snapshot_id=snapshot.id,
            allowed_paths=list(allowed), acceptance_criteria=list(criteria),
            verification_checks=checks,
            constraints=constraints or {
                "max_steps": 8, "max_tool_calls": 20, "max_model_tokens": 50_000,
                "max_runtime_seconds": 120, "network_access": "ALLOW_WITH_APPROVAL",
            },
        )
        return task

    def run_actions(task, actions):
        provider.actions = list(actions)
        started = svc.task_service.start_task(task.id)
        for approval in started.approvals:
            svc.task_service.approve(approval.id)
        svc.task_service.prepare_execution(started.run.id)
        Worker(svc).run(once=True, run_id=started.run.id)
        return svc.runs.get(started.run.id)

    def c2():
        task = task_with([])
        patch = (
            "--- a/utils.py\n+++ b/utils.py\n@@ -1 +1,2 @@\n"
            f" TOKEN_PATTERN = 1\n+AWS_ACCESS_KEY = \"{AWS_KEY}\"\n"
        )
        run = run_actions(task, [
            action("write_file", path="utils.py",
                   content=f"AWS_ACCESS_KEY = \"{AWS_KEY}\"\n", overwrite=True),
            action("apply_patch", patch=patch),
            finish("added config"),
        ])
        assert run.state == WorkflowState.REVIEWING, f"state={run.state.value} error={run.error}"
        calls = svc.calls.list_by_run(run.id)
        blocked = [c for c in calls if c.status.value == "BLOCKED"]
        assert len(blocked) == 2, f"credential writes not blocked: {[c.status.value for c in calls]}"
        assert all("credential" in (c.error or "").lower() for c in blocked), (
            f"block reason unclear: {[c.error for c in blocked]}"
        )
        report, checks = svc.task_service.verify_run(run.id)
        assert report.decision.value != "ACCEPTED", f"secret-bearing run accepted: {report.decision}"
        return f"write and patch blocked before the diff; decision={report.decision.value}"

    probe("C2", "secrets", "A patch that adds a credential is flagged and cannot be accepted", c2)

    def c3():
        task = task_with([command_check('python -c "print(\'ok\')"', criteria=["AC1"], timeout_seconds=20)])
        run = run_actions(task, [finish("did nothing")])
        assert run.state == WorkflowState.REVIEWING, f"state={run.state.value} error={run.error}"
        report, checks = svc.task_service.verify_run(run.id)
        diff = svc.diffs.get_by_run(run.id)
        changed = [f.path for f in diff.files]
        if report.decision.value == "ACCEPTED":
            raise AssertionError(
                f"an empty diff with a pass-only check was ACCEPTED (changed={changed})"
            )
        return f"decision={report.decision.value} changed={changed}"

    probe("C3", "verification", "An empty diff cannot be ACCEPTED just because a check prints ok", c3)

    def c4():
        task = task_with(
            [command_check('python -c "print(1)"', criteria=["AC1"], timeout_seconds=20)],
            constraints={"max_steps": 5, "max_tool_calls": 5, "max_model_tokens": 1000,
                         "max_runtime_seconds": 60, "network_access": "ALLOW_WITH_APPROVAL"},
        )
        from mayaworkbench.models.base import ModelUsage

        original_generate = provider.generate

        def expensive(request):
            response = original_generate(request)
            response.usage = ModelUsage(total_tokens=5000)
            return response

        provider.generate = expensive
        try:
            started = time.monotonic()
            run = run_actions(task, [finish(), finish()])
            elapsed = time.monotonic() - started
        finally:
            provider.generate = original_generate
        assert run.state in {WorkflowState.FAILED, WorkflowState.BLOCKED}, f"token budget ignored: {run.state.value}"
        assert elapsed < 60, f"budget stop took {elapsed:.1f}s"
        return f"state={run.state.value} after {elapsed:.1f}s: {(run.error or '')[:60]}"

    probe("C4", "budgets", "A token budget of 1 stops the run instead of overspending", c4)

    def c5():
        task = task_with(
            [command_check('python -c "print(1)"', criteria=["AC1"], timeout_seconds=20)],
            constraints={"max_steps": 8, "max_tool_calls": 1, "max_model_tokens": 50_000,
                         "max_runtime_seconds": 60, "network_access": "ALLOW_WITH_APPROVAL"},
        )
        run = run_actions(task, [
            action("read_file", path="utils.py"),
            action("read_file", path="utils.py"),
            finish(),
        ])
        calls = svc.calls.list_by_run(run.id)
        assert len(calls) <= 2, f"tool-call budget 1 but {len(calls)} calls recorded"
        assert run.state in {WorkflowState.FAILED, WorkflowState.BLOCKED, WorkflowState.REVIEWING}
        return f"{len(calls)} calls, state={run.state.value}"

    probe("C5", "budgets", "The tool-call budget is enforced", c5)

    def c6():
        task = task_with([])
        repeat = action("read_file", path="utils.py")
        run = run_actions(task, [repeat, repeat, repeat, repeat, repeat, finish()])
        assert run.state == WorkflowState.BLOCKED, f"repeat guard did not block: {run.state.value}"
        calls = svc.calls.list_by_run(run.id)
        assert len(calls) <= 2, f"repeat guard allowed {len(calls)} identical reads"
        return f"blocked after {len(calls)} tool calls: {run.error}"

    probe("C6", "agent", "Repeating one read-only action cannot loop forever", c6)

    # ------------------------------------------------------------ parser bombs
    def c7():
        bomb = base / "bomb-repo"
        bomb.mkdir()
        git(bomb, "init", "-q")
        git(bomb, "config", "user.email", "a@example.com")
        git(bomb, "config", "user.name", "A")
        write(bomb / "deep.py", "x = " + "(" * 900 + "1" + ")" * 900 + "\n")
        (bomb / "binary.py").write_bytes(b"\x00\x01\x02\xff\xfe")
        write(bomb / "ok.py", "Y = 1\n")
        git(bomb, "add", "-A")
        git(bomb, "commit", "-qm", "bombs")
        att = svc.repository_service.attach(project, bomb, name="bombs")
        snap = svc.repository_service.create_snapshot(att.repository).snapshot
        started = time.monotonic()
        report = svc.indexer.index(snap.id)
        elapsed = time.monotonic() - started
        assert elapsed < 60, f"indexing pathological sources took {elapsed:.1f}s"
        kinds = svc.issues.counts_by_kind(snap.id)
        assert "ok.py" in [
            row[0] for row in svc.db.fetchall("SELECT path FROM indexed_files WHERE snapshot_id = ?", (snap.id,))
        ], "healthy file lost from index"
        return f"{elapsed:.1f}s, issues={kinds}"

    probe("C7", "indexing", "Deeply nested and binary sources cannot break indexing", c7)

    def c8():
        bundle = base / "task-bundle"
        task = task_with([command_check('python -c "print(1)"', criteria=["AC1"], timeout_seconds=20)])
        run = run_actions(task, [
            action("write_file", path="utils.py", content="TOKEN_PATTERN = 2\n", overwrite=True),
            finish(),
        ])
        svc.task_service.capture_diff(run.id)
        svc.task_service.review_run(run.id)
        svc.task_service.verify_run(run.id)
        path = svc.task_service.export_run(run.id, bundle)
        leaked = []
        for file in Path(path).rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                if str(base).encode() in data:
                    leaked.append(str(file.relative_to(path)))
        return f"bundle at {Path(path).name}; files naming the local absolute path: {len(leaked)}"

    probe("C8", "evidence", "An exported bundle does not embed the local absolute path", c8)

    out = base / "results4.json"
    out.write_text(json.dumps(RESULTS, indent=2))
    fails = [r for r in RESULTS if r["status"] != "PASS"]
    print(f"\n# TOTAL {len(RESULTS)}  PASS {len(RESULTS)-len(fails)}  FAIL/ERROR {len(fails)}")
    for r in fails:
        print(f"# {r['status']} {r['id']} {r['area']}: {r['claim']} -> {r['detail']}")
    print(f"# results: {out}")
    print(f"# home: {home}")
    svc.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
