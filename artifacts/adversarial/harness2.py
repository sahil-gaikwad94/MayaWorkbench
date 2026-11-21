"""Adversarial audit harness round 2: agent, verification, evidence, recovery."""
from __future__ import annotations

import json
import os
import sqlite3
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
from mayaworkbench.domain.enums import ApprovalAction, TaskType, WorkflowState  # noqa: E402
from mayaworkbench.errors import MayaError  # noqa: E402
from mayaworkbench.models.gateway import ModelGateway  # noqa: E402
from mayaworkbench.tasks.service import TaskService  # noqa: E402
from mayaworkbench.verification.bundle import verify_bundle  # noqa: E402
from mayaworkbench.verification.registry import command_check  # noqa: E402
from mayaworkbench.worker import Worker  # noqa: E402
from tests.conftest import (  # noqa: E402
    ScriptedTaskProvider,
    enable_virtual_model,
    git,
    write,
)
from artifacts.adversarial._cli import maya_argv  # noqa: E402

RESULTS: list[dict] = []


def probe(pid: str, area: str, claim: str, fn):
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
    RESULTS.append(
        {"id": pid, "area": area, "claim": claim, "status": status, "detail": detail,
         "ms": round((time.monotonic() - started) * 1000)}
    )
    print(f"[{status:5}] {pid} {claim} :: {detail}")
    return status


UTILS = '''"""Utilities."""

import re

TOKEN_PATTERN = re.compile(r"\\w+")


def count_tokens(text: str) -> int:
    """Count tokens."""
    return len(TOKEN_PATTERN.findall(text))
'''

TEST = '''from utils import count_tokens


def test_count():
    assert count_tokens("a b") == 2
'''


def build_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "audit@example.com")
    git(root, "config", "user.name", "Audit")
    git(root, "config", "commit.gpgsign", "false")
    write(root / "utils.py", UTILS)
    write(root / "tests" / "test_utils.py", TEST)
    write(root / "README.md", "# Repo\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def finish(summary="done"):
    return {
        "thought": "done",
        "tool": "finish",
        "arguments": {"summary": summary, "limitations": [], "checks_run": [], "remaining_work": []},
    }


def action(tool, **arguments):
    return {"thought": f"try {tool}", "tool": tool, "arguments": arguments}


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="maya-audit2-"))
    home = base / "home"
    repo = build_repo(base)
    svc = Services.open(str(home))
    project = svc.create_project("Audit2", "")
    attached = svc.repository_service.attach(project, repo)
    snapshot = svc.repository_service.create_snapshot(attached.repository).snapshot
    svc.indexer.index(snapshot.id)

    provider = ScriptedTaskProvider()

    def install_model():
        enable_virtual_model(home)
        os.environ["MAYA_MODEL_API_KEY"] = "test-key"
        svc.settings = load_settings(AppPaths.resolve(str(home)))
        gateway = ModelGateway(svc.settings, svc.invocations, svc.events, lambda _s: provider)
        svc.gateway = gateway
        for service in (svc.plan_service, svc.task_service):
            service.gateway = gateway
            service.settings = svc.settings
        return gateway

    install_model()

    def create(checks, *, allowed=("utils.py", "tests"), max_steps=8, repair=None):
        constraints = {
            "max_steps": max_steps,
            "max_tool_calls": 20,
            "max_model_tokens": 20_000,
            "max_runtime_seconds": 120,
            "network_access": "ALLOW_WITH_APPROVAL",
        }
        if repair is not None:
            constraints["max_repair_attempts"] = repair
        task, contract = svc.task_service.create_task(
            project_id=project.id,
            title="audit task",
            goal="satisfy the declared check",
            task_type=TaskType.ENGINEERING_CHANGE,
            snapshot_id=snapshot.id,
            allowed_paths=list(allowed),
            acceptance_criteria=["AC1 is satisfied"],
            verification_checks=checks,
            constraints=constraints,
        )
        return task, contract

    def run_actions(actions, task):
        provider.actions = list(actions)
        started = svc.task_service.start_task(task.id)
        for approval in started.approvals:
            svc.task_service.approve(approval.id)
        svc.task_service.prepare_execution(started.run.id)
        Worker(svc).run(once=True, run_id=started.run.id)
        return svc.runs.get(started.run.id)

    marker_check = command_check(
        'python -c "from utils import marker; assert marker == 42; print(\'ok\')"',
        criteria=["AC1"],
        timeout_seconds=30,
    )
    trivial_check = command_check(
        'python -c "print(\'ok\')"', criteria=["AC1"], timeout_seconds=30
    )

    # D1: malformed tool arguments must not abort the loop with an exception
    def d1():
        # No declared check: the only thing ending this run should be `finish`
        # after the loop rejected the invalid write, not the invalid write itself.
        task, _ = create([])
        crashed = None
        try:
            run = run_actions(
                [action("write_file", path="../evil.py", content="x"), finish()], task
            )
        except Exception as exc:  # noqa: BLE001
            crashed = f"{type(exc).__name__}: {exc}"
            run = svc.task_service.latest_run_for_task(task.id)
        assert crashed is None, f"tool-argument validation escaped the loop: {crashed}"
        assert run is not None, "no run recorded"
        assert run.state != WorkflowState.FAILED, (
            f"a recoverable argument error ended the run: {run.error}"
        )
        return f"state={run.state.value}"

    probe("D1", "agent", "Malformed tool arguments are handled inside the loop", d1)

    def d2():
        task, _ = create([trivial_check], max_steps=6)
        crashed = None
        try:
            run = run_actions(
                [action("definitely_not_a_tool", x=1)] * 3 + [finish()], task
            )
        except Exception as exc:  # noqa: BLE001
            crashed = f"{type(exc).__name__}: {exc}"
            run = svc.task_service.latest_run_for_task(task.id)
        assert crashed is None, f"invalid tool enum crashed the loop: {crashed}"
        assert run.state == WorkflowState.FAILED, f"expected FAILED, got {run.state.value}"
        assert "invalid action" in (run.error or "").lower(), f"error={run.error}"
        return "three invalid actions end in a typed FAILED"

    probe("D2", "agent", "Invalid actions are rejected with a bounded retry", d2)

    def d3():
        task, _ = create([marker_check], repair=1)
        run = run_actions([finish("nothing changed"), finish("still nothing")], task)
        assert run.state == WorkflowState.REVIEWING, f"state={run.state.value} error={run.error}"
        report, checks = svc.task_service.verify_run(run.id)
        failing = [c for c in checks if c.outcome.value not in {"PASSED", "NOT_APPLICABLE", "SKIPPED"}]
        assert failing, "no failing check recorded for a no-op run"
        assert report.decision.value != "ACCEPTED", f"no-op run accepted: {report.decision}"
        return f"decision={report.decision.value} failing={[c.key for c in failing]}"

    probe("D3", "verification", "A no-op run cannot be ACCEPTED, whatever finish claims", d3)

    def d4():
        task, _ = create([marker_check])
        patched = UTILS + "\nmarker = 42\n"
        run = run_actions(
            [action("write_file", path="utils.py", content=patched, overwrite=True), finish("added marker")],
            task,
        )
        assert run.state == WorkflowState.REVIEWING, f"error={run.error}"
        report, checks = svc.task_service.verify_run(run.id)
        assert report.decision.value == "ACCEPTED", f"decision={report.decision} blocking={report.blocking}"
        return f"accepted; diff={svc.diffs.get_by_run(run.id).files[0].path}"

    probe("D4", "verification", "A correct patch with a declared check is ACCEPTED", d4)

    def d5():
        # Contract tampering after approval must invalidate verification.
        task, _ = create([trivial_check])
        run = run_actions([finish()], task)
        svc.task_service.verify_run(run.id)
        contract_id = svc.runs.get(run.id).contract_id
        original = svc.db.fetchone(
            "SELECT content_sha256 FROM task_contracts WHERE id = ?", (contract_id,)
        )[0]
        svc.db.execute(
            "UPDATE task_contracts SET content_sha256 = ? WHERE id = ?",
            ("0" * 64, contract_id),
        )
        try:
            try:
                report, _ = svc.task_service.verify_run(run.id)
            except MayaError as exc:
                return f"tampered contract refused: {type(exc).__name__}"
        finally:
            svc.db.execute(
                "UPDATE task_contracts SET content_sha256 = ? WHERE id = ?",
                (original, contract_id),
            )
        assert report.decision.value != "ACCEPTED", "tampered contract still ACCEPTED"
        return f"tampered contract decision={report.decision.value}"

    probe("D5", "verification", "Editing the contract's checks invalidates the evidence", d5)

    def d6():
        task, _ = create(
            [trivial_check], allowed=("utils.py", "tests", "tests/test_utils.py")
        )
        weakened = 'from utils import count_tokens\n\n\ndef test_count():\n    assert True\n'
        run = run_actions(
            [
                action("write_file", path="tests/test_utils.py", content=weakened, overwrite=True),
                action("write_file", path="utils.py", content=UTILS + "\nX = 1\n", overwrite=True),
                finish(),
            ],
            task,
        )
        assert run.state == WorkflowState.REVIEWING, f"error={run.error}"
        report, checks = svc.task_service.verify_run(run.id)
        integrity = [c for c in checks if c.key == "test.integrity"]
        assert integrity, "no test.integrity check recorded"
        assert integrity[0].outcome.value == "FAILED", (
            f"weakened test not flagged: {integrity[0].outcome} {integrity[0].summary}"
        )
        assert report.decision.value != "ACCEPTED", f"decision={report.decision.value}"
        return f"integrity={integrity[0].outcome.value} decision={report.decision.value}"

    probe("D6", "verification", "A silently weakened test is caught", d6)

    def d7():
        broken = command_check(
            "python -m pytest --definitely-not-a-flag",
            criteria=["AC1"],
            timeout_seconds=30,
        )
        task, _ = create([broken])
        run = run_actions([finish()], task)
        assert run.state == WorkflowState.REVIEWING, f"state={run.state} error={run.error}"
        report, checks = svc.task_service.verify_run(run.id)
        check = next(c for c in checks if c.key == broken.key)
        assert check.outcome.value != "PASSED", f"broken checker passed: {check.outcome}"
        assert report.decision.value != "ACCEPTED", f"broken checker accepted: {report.decision}"
        return f"outcome={check.outcome.value} decision={report.decision.value}"

    probe("D7", "verification", "A checker that cannot produce a verdict is never a pass", d7)

    def d8():
        # In-loop pass must not replace re-execution; a stateful check fails on rerun.
        stateful = command_check(
            'python -c "import pathlib; p=pathlib.Path(\'seen.txt\'); assert not p.exists(); p.write_text(\'1\')"',
            criteria=["AC1"],
            timeout_seconds=30,
        )
        task, _ = create([stateful])
        run = run_actions([finish()], task)
        assert run.state == WorkflowState.REVIEWING, f"error={run.error}"
        details = svc.runs.get(run.id).details
        assert details.get("in_loop_verdict") == "PASSED", f"in-loop={details.get('in_loop_verdict')}"
        report, _ = svc.task_service.verify_run(run.id)
        assert report.decision.value != "ACCEPTED", "in-loop PASS was reused instead of re-execution"
        return f"in-loop PASS, verify={report.decision.value}"

    probe("D8", "verification", "In-loop passes are advisory, verification re-executes", d8)

    # ------------------------------------------------------------ evidence
    accepted_task, _ = create([marker_check])
    accepted_run = run_actions(
        [action("write_file", path="utils.py", content=UTILS + "\nmarker = 42\n"), finish()],
        accepted_task,
    )
    svc.task_service.verify_run(accepted_run.id)
    bundle_dir = base / "bundle"
    svc.verification.export_bundle(accepted_run.id, bundle_dir)

    def e1():
        result = verify_bundle(bundle_dir)
        assert result.ok, f"fresh bundle failed verification: {result.errors}"
        return "fresh bundle verifies"

    probe("E1", "evidence", "A freshly exported bundle verifies", e1)

    def e2():
        target = sorted(bundle_dir.rglob("report.json"))[0]
        original = target.read_bytes()
        target.write_bytes(original + b" \n")
        try:
            result = verify_bundle(bundle_dir)
            assert not result.ok, "tampered report still verifies"
            return f"detected: {result.errors[:1]}"
        finally:
            target.write_bytes(original)

    probe("E2", "evidence", "A modified report inside the bundle is detected", e2)

    def e3():
        files = [p for p in bundle_dir.rglob("*") if p.is_file() and p.name != "manifest.json"]
        victim = files[0]
        original = victim.read_bytes()
        victim.unlink()
        try:
            result = verify_bundle(bundle_dir)
            assert not result.ok, "missing file not detected"
            return f"detected missing {victim.name}"
        finally:
            victim.write_bytes(original)

    probe("E3", "evidence", "A removed bundle file is detected", e3)

    def e4():
        extra = bundle_dir / "extra.txt"
        extra.write_text("smuggled")
        try:
            result = verify_bundle(bundle_dir)
            assert not result.ok, "extra file not detected"
            return f"detected extra file: {result.errors[:1]}"
        finally:
            extra.unlink()

    probe("E4", "evidence", "An extra file in the bundle is detected", e4)

    def e5():
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        text = json.dumps(manifest)
        assert "sha256" in text, "manifest has no hashes"
        # flip a recorded hash
        mutated = json.loads(text)
        target = None
        for key, value in mutated.items():
            if isinstance(value, list) and value and isinstance(value[0], dict) and "sha256" in value[0]:
                value[0]["sha256"] = "0" * 64
                target = key
                break
        assert target, "no hash list found in manifest"
        (bundle_dir / "manifest.json").write_text(json.dumps(mutated))
        try:
            result = verify_bundle(bundle_dir)
            assert not result.ok, "mutated manifest sha not detected"
            return f"detected manifest hash change in {target}"
        finally:
            (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

    probe("E5", "evidence", "A rewritten manifest hash is detected", e5)

    # ------------------------------------------------------------ recovery
    def f1():
        task, _ = create([trivial_check], max_steps=3)
        provider.actions = [finish()]
        started = svc.task_service.start_task(task.id)
        for approval in started.approvals:
            svc.task_service.approve(approval.id)
        svc.task_service.prepare_execution(started.run.id)
        job = svc.jobs.get_by_run(started.run.id)
        assert job is not None, "no worker job created"
        svc.db.execute(
            "UPDATE worker_jobs SET status = 'LEASED', lease_owner = 'dead-worker', "
            "lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job.id,),
        )
        Worker(svc).run(once=True, run_id=started.run.id)
        run = svc.runs.get(started.run.id)
        assert run.state == WorkflowState.REVIEWING, f"stale lease not reclaimed: {run.state.value}"
        return "expired lease reclaimed by a live worker"

    probe("F1", "recovery", "A stalled worker's expired lease is reclaimed", f1)

    def f2():
        blink = base / "blink"
        blink.mkdir()
        git(blink, "init", "-q")
        git(blink, "config", "user.email", "a@example.com")
        git(blink, "config", "user.name", "A")
        write(blink / "x.py", "X = 1\n")
        git(blink, "add", "-A")
        git(blink, "commit", "-qm", "x")
        att = svc.repository_service.attach(project, blink, name="blink")
        empty = base / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        att2 = svc.repository_service.attach(project, empty, name="empty")
        try:
            svc.repository_service.create_snapshot(att2.repository)
        except MayaError as exc:
            typed = type(exc).__name__
        else:
            raise AssertionError("snapshot of an empty repository succeeded")
        notgit = base / "notgit"
        notgit.mkdir()
        try:
            svc.repository_service.attach(project, notgit, name="notgit")
        except MayaError as exc:
            return f"empty={typed}, non-git={type(exc).__name__}"
        raise AssertionError("non-git directory was attached")

    probe("F2", "onboarding", "Empty and non-Git repositories fail with typed errors", f2)

    def f3():
        target = repo / "utils.py"
        original = target.read_text()
        target.write_text(original + "\nWORKTREE_PINNED_MARKER = 7\n")
        try:
            outcome = svc.repository_service.create_snapshot(
                attached.repository, working_tree="worktree"
            )
            svc.indexer.index(outcome.snapshot.id)
            hits, _ = svc.search.search_text(outcome.snapshot.id, "WORKTREE_PINNED_MARKER")
            assert hits, "worktree snapshot did not include uncommitted content"
            return f"kind={outcome.snapshot.kind.value} patch={bool(outcome.snapshot.includes_worktree_patch)}"
        finally:
            target.write_text(original)

    probe("F3", "onboarding", "A worktree snapshot pins and indexes uncommitted content", f3)

    def f4():
        procs = []
        for i in range(6):
            procs.append(
                subprocess.Popen(
                    maya_argv(
                        "--home", str(home),
                        "memory", "add",
                        "--project", project.id,
                        "--kind", "FACT",
                        "--title", f"concurrent {i}",
                        "--statement", f"writer {i}",
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        outputs = [(p.wait(timeout=60), p.communicate()) for p in procs]
        failed = [(code, err[-200:]) for code, (_, err) in outputs if code != 0]
        assert not failed, f"concurrent writers failed: {failed}"
        count = svc.db.fetchone(
            "SELECT COUNT(*) FROM project_memories WHERE project_id = ?", (project.id,)
        )[0]
        assert count >= 6, f"only {count} concurrent memory rows"
        return f"6/6 writers succeeded, rows={count}"

    probe("F4", "concurrency", "Concurrent processes write without lock failures", f4)

    # ------------------------------------------------------------ research/QA
    def h1():
        from mayaworkbench.models.base import ModelCapabilities, ModelResponse, ModelUsage

        class Forged:
            name = "fake"
            model = "fake-1"

            def capabilities(self):
                return ModelCapabilities(structured_output=True)

            def generate(self, request):
                payload = {
                    "answer_markdown": "The definition is in utils.py.",
                    "citations": [
                        {
                            "path": "utils.py",
                            "start_line": 1,
                            "end_line": 2,
                            "quote": "def totally_invented():",
                            "reason": "forged",
                        },
                        {
                            "path": "invented/nowhere.py",
                            "start_line": 1,
                            "end_line": 1,
                            "quote": "INVENTED_PATH_QUOTE",
                            "reason": "forged path",
                        },
                    ],
                    "uncertainties": [],
                    "unsupported_claims": [],
                }
                return ModelResponse(
                    text=json.dumps(payload),
                    structured=payload,
                    usage=ModelUsage(total_tokens=10),
                    provider="fake",
                    model="fake-1",
                    latency_ms=1,
                )

        gateway = ModelGateway(svc.settings, svc.invocations, svc.events, lambda _s: Forged())
        svc.ask.gateway = gateway
        svc.ask.settings = svc.settings
        result = svc.ask.ask(snapshot.id, "Where is count_tokens defined?")
        record = result.record
        assert record.status.value == "CITATIONS_REJECTED", f"status={record.status.value}"
        assert "withheld" in record.answer_markdown.lower(), "answer was not withheld"
        assert record.citations and all(not c.valid for c in record.citations), (
            f"rejected citations not marked invalid: {[c.valid for c in record.citations]}"
        )
        assert "totally_invented" not in record.answer_markdown, "forged quote rendered in the answer"
        return (
            f"status={record.status.value}, {len(record.citations)} citations rejected "
            f"with reasons, answer withheld"
        )

    probe("H1", "qa", "Citations the code cannot ground are rejected, not rendered", h1)

    def h2():
        other = svc.create_project("Other", "")
        svc.memory.add(
            project_id=project.id,
            kind=__import__("mayaworkbench.domain.enums", fromlist=["MemoryKind"]).MemoryKind.FACT,
            title="project A fact",
            statement="only for A",
        )
        rows = svc.memory.list(project_id=other.id)
        leaked = [m for m in rows if m.title == "project A fact"]
        assert not leaked, "memory leaked across projects"
        try:
            svc.memory.add(
                project_id=project.id,
                kind=__import__("mayaworkbench.domain.enums", fromlist=["MemoryKind"]).MemoryKind.FACT,
                title="leak attempt",
                statement="token sk-live-abcdef0123456789abcdef0123456789",
            )
        except MayaError:
            return f"isolated; {len(rows)} rows in other project; secret refused"
        raise AssertionError("secret-shaped memory statement accepted")

    probe("H2", "memory", "Memory is project-scoped and refuses secret-shaped text", h2)

    def g7():
        cwd = base / "init-cwd"
        cwd.mkdir()
        first = subprocess.run(
            maya_argv("--home", str(base / "init-home"), "init", "--project", "twice"),
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        second = subprocess.run(
            maya_argv("--home", str(base / "init-home"), "init", "--project", "twice"),
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        assert first.returncode == 0, first.stderr[-300:]
        assert second.returncode == 0, f"second init failed: {second.stderr[-300:]}"
        assert "Traceback" not in second.stderr, "init twice raised a traceback"
        con = sqlite3.connect(base / "init-home" / "maya.db")
        n = con.execute("SELECT COUNT(*) FROM projects WHERE name = 'twice'").fetchone()[0]
        assert n == 1, f"init twice created {n} projects"
        return "idempotent, one project"

    probe("G7", "onboarding", "`maya init` twice is idempotent", g7)

    out = base / "results2.json"
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
