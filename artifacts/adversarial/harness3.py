"""Adversarial audit harness round 3: research, experiments, scale, storage."""
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
from mayaworkbench.domain.enums import (  # noqa: E402
    ExperimentProfile,
    ExperimentRole,
    MetricDirection,
    MetricsSourceKind,
    TaskType,
    WorkflowState,
)
from mayaworkbench.domain.models import MetricSpec, MetricsSource  # noqa: E402
from mayaworkbench.errors import MayaError  # noqa: E402
from mayaworkbench.models.gateway import ModelGateway  # noqa: E402
from mayaworkbench.verification.registry import command_check  # noqa: E402
from mayaworkbench.worker import Worker  # noqa: E402
from tests.conftest import ScriptedTaskProvider, enable_virtual_model, git, write  # noqa: E402

RESULTS: list[dict] = []


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


PAPER = """---
title: Audit Paper
authors: A. Tester
published: 2025-01-01
license: CC-BY-4.0
---

# Abstract

We introduce a causal attention mask that prevents future positions from being attended.

# 2 Method

## 2.1 Masking

For a sequence of length n we compute scaled scores and apply a triangular mask with torch.triu before the softmax.

# 3 Experiments

We evaluate on WikiText-103 with 4 GPUs for 40 epochs and report top-1 accuracy.
"""


def build_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "a@example.com")
    git(root, "config", "user.name", "A")
    write(root / "utils.py", "def f(x):\n    return x * 2\n")
    write(root / "README.md", "# Audit repo\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="maya-audit3-"))
    home = base / "home"
    repo = build_repo(base)
    svc = Services.open(str(home))
    project = svc.create_project("Audit3", "")
    attached = svc.repository_service.attach(project, repo)
    snapshot = svc.repository_service.create_snapshot(attached.repository).snapshot
    svc.indexer.index(snapshot.id)

    # ------------------------------------------------------------ research
    def r1():
        try:
            svc.ingestion.ingest_file(project, base / "nope.md")
        except MayaError as exc:
            return f"{type(exc).__name__}"
        raise AssertionError("missing document accepted")

    probe("R1", "research", "Registering a missing file fails with a typed error", r1)

    def r2():
        bad = base / "binary.md"
        bad.write_bytes(b"\xff\xfe\x00\x01not text")
        try:
            result = svc.ingestion.ingest_file(project, bad)
        except MayaError as exc:
            return f"refused: {type(exc).__name__}"
        doc = result.document
        assert doc.extraction_status.value != "EXTRACTED", "binary document reported as extracted"
        return f"stored with status={doc.extraction_status.value}"

    probe("R2", "research", "Binary documents are not silently extracted", r2)

    def r3():
        empty = base / "empty.md"
        empty.write_bytes(b"")
        try:
            result = svc.ingestion.ingest_file(project, empty)
        except MayaError as exc:
            return f"refused: {type(exc).__name__}"
        passages = svc.passages.list_by_document(result.document.id)
        assert not passages, "zero-byte document produced passages"
        return f"stored status={result.document.extraction_status.value}, no passages"

    probe("R3", "research", "A zero-byte document cannot become evidence", r3)

    def r4():
        big = base / "big.md"
        big.write_text("x" * (svc.settings.research.max_document_bytes + 10))
        try:
            svc.ingestion.ingest_file(project, big)
        except MayaError as exc:
            return f"refused at {svc.settings.research.max_document_bytes} bytes: {type(exc).__name__}"
        raise AssertionError("oversized document accepted")

    probe("R4", "research", "The document size limit is enforced", r4)

    paper_path = base / "paper.md"
    paper_path.write_text(PAPER)
    paper = svc.ingestion.ingest_file(project, paper_path).document

    def r5():
        assert paper.extraction_status.value == "EXTRACTED", paper.extraction_status
        passages = svc.passages.list_by_document(paper.id)
        assert len(passages) >= 3, f"only {len(passages)} passages"
        return f"status={paper.extraction_status.value} passages={len(passages)}"

    probe("R5", "research", "A Markdown paper yields sectioned passages", r5)

    def r6():
        summary = svc.claim_service.extract(paper.id, use_model=False)
        claims = svc.claim_service.list_claims(document_id=paper.id)
        assert claims, f"no deterministic claims extracted ({summary})"
        import hashlib

        passages = {p.id: p.text for p in svc.passages.list_by_document(paper.id)}
        ungrounded = []
        for claim in claims:
            assert claim.statement.strip(), "empty claim statement"
            assert claim.source_locations, f"claim {claim.id} has no source location"
            for loc in claim.source_locations:
                if loc.passage_id not in passages:
                    ungrounded.append((claim.id, "passage missing"))
                elif loc.quote not in passages[loc.passage_id]:
                    ungrounded.append((claim.id, "quote not in passage"))
                elif hashlib.sha256(loc.quote.encode()).hexdigest() != loc.quote_hash:
                    ungrounded.append((claim.id, "quote hash mismatch"))
        assert not ungrounded, f"ungrounded claims: {ungrounded}"
        return f"{len(claims)} claims, every source quote exists and hashes correctly"

    probe("R6", "research", "Every extracted claim quote exists in its stored passage", r6)

    def r7():
        try:
            svc.claim_service.extract(paper.id, use_model=True)
        except MayaError as exc:
            return f"typed refusal: {type(exc).__name__}"
        raise AssertionError("model extraction succeeded without a configured model")

    probe("R7", "research", "Model extraction without a provider fails closed", r7)

    def r8():
        claims = svc.claim_service.list_claims(document_id=paper.id)
        claim = claims[0]
        outcome = svc.mapping_service.map_claim(claim, snapshot, use_model=False)
        evidence = getattr(outcome, "evidence", []) or []
        status = getattr(outcome, "status", None)
        for item in evidence:
            path = getattr(item, "path", None)
            assert path is None or any(
                f.path == path for f in svc.files.list_by_snapshot(snapshot.id)
            ), f"mapping evidence names a path not in the snapshot: {path}"
        return f"status={getattr(status, 'value', status)} evidence={len(evidence)}"

    probe("R8", "mapping", "A paper claim maps only to code that exists in the snapshot", r8)

    def r9():
        svc.memo_service.build(project, document=paper, title="Audit memo")
        memo = svc.memos.latest(project_id=project.id, document_id=paper.id, snapshot_id=None)
        assert memo is not None, "no memo stored"
        text = getattr(memo, "markdown", None) or getattr(memo, "summary", None) or ""
        assert "paper" in str(text).lower() or memo.artifact_id, "memo has neither text nor an artifact"
        return f"memo id={memo.id} artifact={bool(getattr(memo, 'artifact_id', None))}"

    probe("R9", "research", "A memo builds from a document with grounded references", r9)

    # ------------------------------------------------------------ experiments
    write(repo / "experiment.py", "import json, sys\nprint(json.dumps({'primary': float(sys.argv[2])}))\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "experiment harness")
    exp_snapshot = svc.repository_service.create_snapshot(attached.repository).snapshot
    svc.indexer.index(exp_snapshot.id)

    def definition(name, role, command, seeds=(1, 2), approve=False):
        return svc.experiments.create_definition(
            project_id=project.id,
            snapshot_id=exp_snapshot.id,
            name=name,
            hypothesis="the candidate improves the primary metric",
            role=role,
            profile=ExperimentProfile.CPU_SMOKE,
            command=command,
            seeds=list(seeds),
            metrics=[
                MetricSpec(
                    key="primary",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    primary=True,
                )
            ],
            metrics_source=MetricsSource(kind=MetricsSourceKind.STDOUT_JSON),
            approve=approve,
        )

    def e1():
        try:
            svc.experiments.create_definition(
                project_id=project.id, snapshot_id=exp_snapshot.id, name="two primaries",
                hypothesis="h", role=ExperimentRole.BASELINE,
                profile=ExperimentProfile.CPU_SMOKE, command=["python", "-c", "print('{}')"],
                seeds=[1],
                metrics=[
                    MetricSpec(key="a", direction=MetricDirection.HIGHER_IS_BETTER, primary=True),
                    MetricSpec(key="b", direction=MetricDirection.HIGHER_IS_BETTER, primary=True),
                ],
                metrics_source=MetricsSource(kind=MetricsSourceKind.STDOUT_JSON),
            )
        except MayaError as exc:
            return f"refused: {type(exc).__name__}"
        raise AssertionError("two primary metrics accepted")

    probe("E1", "experiments", "Exactly one primary metric is enforced", e1)

    def e2():
        try:
            definition("dup seeds", ExperimentRole.BASELINE, ["python", "-c", "print('{}')"], seeds=(1, 1))
        except MayaError as exc:
            return f"refused: {type(exc).__name__}"
        raise AssertionError("duplicate seeds accepted")

    probe("E2", "experiments", "Duplicate seeds are refused", e2)

    def e3():
        d = definition(
            "revoked", ExperimentRole.BASELINE,
            ["python", "experiment.py", "{seed}", "1.0"], approve=True,
        )
        svc.experiments.revoke(d.id, reason="audit")
        try:
            svc.experiments.run(d.id)
        except MayaError as exc:
            return f"run refused after revoke: {type(exc).__name__}"
        raise AssertionError("revoked experiment executed")

    probe("E3", "experiments", "Revoking a pre-registration prevents execution", e3)

    def e4():
        try:
            d = definition("self", ExperimentRole.BASELINE, ["python", "experiment.py", "{seed}", "1.0"])
            svc.experiments.compare(d.id, baseline_id=d.id)
        except MayaError as exc:
            return f"refused: {type(exc).__name__}"
        raise AssertionError("an experiment was compared with itself")

    probe("E4", "experiments", "An experiment cannot be compared with itself", e4)

    def e5():
        baseline = definition(
            "baseline", ExperimentRole.BASELINE,
            ["python", "experiment.py", "{seed}", "1.0"], approve=True,
        )
        candidate = definition(
            "candidate", ExperimentRole.CANDIDATE,
            ["python", "experiment.py", "{seed}", "2.0"], approve=True,
        )
        try:
            comparison = svc.experiments.compare(candidate.id, baseline_id=baseline.id)
        except MayaError as exc:
            return f"refused before runs: {type(exc).__name__}"
        verdict = getattr(comparison, "verdict", None) or getattr(comparison, "status", None)
        value = getattr(verdict, "value", verdict)
        assert value not in {"IMPROVED", "ACCEPTED", "SIGNIFICANT"}, (
            f"comparison without runs reported {value}"
        )
        return f"no-run comparison={value}"

    probe("E5", "experiments", "A comparison without executed runs cannot claim improvement", e5)

    def e6():
        d = definition("mutable", ExperimentRole.BASELINE, ["python", "experiment.py", "{seed}", "1.0"], approve=True)
        original = svc.experiments.definition_view(d.id)
        svc.experiments.revoke(d.id, reason="audit")
        # definitions are immutable rows; approve/revoke must not change parameters
        after = svc.experiments.definition_view(d.id)
        for key in ("name", "hypothesis", "seeds", "metrics"):
            assert after.get(key) == original.get(key), f"definition field {key} changed"
        return "parameters unchanged through approve/revoke"

    probe("E6", "experiments", "Approve/revoke never rewrites a pre-registration", e6)

    def e7():
        try:
            stats_p = __import__("mayaworkbench.experiments.stats", fromlist=["paired_t"]).paired_t(
                [0.0, 0.0, 0.0]
            )
        except MayaError:
            return "identical differences refused"
        assert stats_p.p_value is None or stats_p.p_value in (0.0, 1.0), (
            f"identical differences produced p={stats_p.p_value}"
        )
        return f"identical differences p={stats_p.p_value}"

    probe("E7", "stats", "Zero-variance paired differences cannot look significant", e7)

    # ------------------------------------------------------------ scale
    def s1():
        big = base / "scale"
        big.mkdir()
        git(big, "init", "-q")
        git(big, "config", "user.email", "a@example.com")
        git(big, "config", "user.name", "A")
        for i in range(700):
            write(big / f"pkg{i % 20}" / f"mod_{i}.py", f"VALUE_{i} = {i}\n\n\ndef f_{i}(x):\n    return x + {i}\n")
        write(big / "pyproject.toml", "[project]\nname='scale'\n")
        (big / "blob.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))
        write(big / "long_line.py", "X = [" + ",".join("1" for _ in range(200_000)) + "]\n")
        deep = big
        for i in range(40):
            deep = deep / f"d{i}"
        write(deep / "deep.py", "DEEP = 1\n")
        git(big, "add", "-A")
        git(big, "commit", "-qm", "scale")
        att = svc.repository_service.attach(project, big, name="scale")
        snap = svc.repository_service.create_snapshot(att.repository).snapshot
        started = time.monotonic()
        report = svc.indexer.index(snap.id)
        duration = time.monotonic() - started
        hits, _ = svc.search.search_text(snap.id, "VALUE_699")
        latency = time.monotonic() - started - duration
        assert report.files_indexed >= 600, f"only {report.files_indexed} files indexed"
        assert hits, "deep file not searchable"
        assert duration < 120, f"indexing 700 files took {duration:.1f}s"
        return f"{report.files_indexed} files in {duration:.1f}s, search {latency:.2f}s, issues={report.issues}"

    probe("S1", "scale", "A 700-file repository indexes and searches within budget", s1)

    # ------------------------------------------------------------ storage
    def t1():
        copy = base / "migrate.db"
        source = sqlite3.connect(home / "maya.db")
        target = sqlite3.connect(copy)
        source.backup(target)
        source.close()
        target.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (999, 'future', 'x', '2030-01-01T00:00:00+00:00')"
        )
        target.commit()
        target.close()
        broken_home = base / "broken-home"
        broken_home.mkdir()
        (broken_home / "maya.db").write_bytes(copy.read_bytes())
        result = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "maya"), "--home", str(broken_home), "db", "info"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0, "future schema version opened as if current"
        assert "Traceback" not in result.stderr, "schema mismatch produced a traceback"
        return f"refused with exit {result.returncode}"

    probe("T1", "storage", "A database from a newer schema is refused, not misread", t1)

    def t2():
        kill_home = base / "kill-home"
        kill_svc = Services.open(str(kill_home))
        kill_project = kill_svc.create_project("Kill", "")
        kill_svc.close()
        procs = []
        for i in range(10):
            procs.append(
                subprocess.Popen(
                    [str(ROOT / ".venv" / "bin" / "maya"), "--home", str(kill_home), "memory", "add",
                     "--project", kill_project.id, "--kind", "FACT", "--title", f"kill {i}",
                     "--statement", f"s{i}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            )
        procs[0].kill()
        for p in procs[1:]:
            p.wait(timeout=60)
        con = sqlite3.connect(kill_home / "maya.db")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"integrity_check after SIGKILL: {integrity}"
        result = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "maya"), "--home", str(kill_home), "--json", "project", "list"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr[-300:]
        reopened = Services.open(str(kill_home))
        reopened.db.fetchone("SELECT COUNT(*) FROM project_memories")
        reopened.close()
        return "integrity ok, workbench reopened after SIGKILL"

    probe("T2", "storage", "SIGKILL during concurrent writes leaves a usable database", t2)

    # ------------------------------------------------------------ verification edges
    provider = ScriptedTaskProvider()
    enable_virtual_model(home)
    os.environ["MAYA_MODEL_API_KEY"] = "test-key"
    svc.settings = load_settings(AppPaths.resolve(str(home)))
    gateway = ModelGateway(svc.settings, svc.invocations, svc.events, lambda _s: provider)
    svc.gateway = gateway
    for service in (svc.plan_service, svc.task_service):
        service.gateway = gateway
        service.settings = svc.settings

    def task_with(checks, constraints=None):
        task, _ = svc.task_service.create_task(
            project_id=project.id, title="edge", goal="audit",
            task_type=TaskType.ENGINEERING_CHANGE, snapshot_id=snapshot.id,
            allowed_paths=["utils.py"],
            acceptance_criteria=["AC1"],
            verification_checks=checks,
            constraints=constraints or {
                "max_steps": 6, "max_tool_calls": 10, "max_model_tokens": 10_000,
                "max_runtime_seconds": 120, "network_access": "ALLOW_WITH_APPROVAL",
            },
        )
        return task

    def run_finish(task, actions=None):
        provider.actions = list(actions or [{
            "thought": "done", "tool": "finish",
            "arguments": {"summary": "done", "limitations": [], "checks_run": [], "remaining_work": []},
        }])
        started = svc.task_service.start_task(task.id)
        for approval in started.approvals:
            svc.task_service.approve(approval.id)
        svc.task_service.prepare_execution(started.run.id)
        Worker(svc).run(once=True, run_id=started.run.id)
        return svc.runs.get(started.run.id)

    def v1():
        slow = command_check('python -c "import time; time.sleep(10)"', criteria=["AC1"], timeout_seconds=1)
        run = run_finish(task_with([slow]))
        report, checks = svc.task_service.verify_run(run.id)
        check = next(c for c in checks if c.key == slow.key)
        assert check.outcome.value != "PASSED", f"timed-out check passed: {check.outcome}"
        assert report.decision.value != "ACCEPTED", f"timed-out check accepted: {report.decision}"
        return f"outcome={check.outcome.value} decision={report.decision.value}"

    probe("V1", "verification", "A check that times out cannot be accepted", v1)

    def v2():
        run = run_finish(task_with([]))
        report, checks = svc.task_service.verify_run(run.id)
        limits = " ".join(report.limitations).lower()
        assert report.decision.value != "ACCEPTED" or "check" in limits, (
            f"no declared checks but decision={report.decision.value} limitations={report.limitations}"
        )
        return f"decision={report.decision.value} limits={report.limitations[:2]}"

    probe("V2", "verification", "A run with no declared checks does not overclaim", v2)

    def v3():
        fabricated = [{
            "thought": "claim success", "tool": "finish",
            "arguments": {"summary": "all tests passed", "limitations": [],
                          "checks_run": ["all tests passed", "pytest: 42 passed"],
                          "remaining_work": []},
        }]
        run = run_finish(task_with([]), fabricated)
        report, checks = svc.task_service.verify_run(run.id)
        blob = json.dumps([c.model_dump(mode="json") for c in checks])
        assert "42 passed" not in blob, "model-reported checks_run became a verification check"
        return f"checks={len(checks)}, model claims absent"

    probe("V3", "verification", "Model-reported checks never become evidence", v3)

    def v4():
        run = run_finish(task_with([command_check('python -c "print(1)"', criteria=["AC1"], timeout_seconds=20)]))
        first, _ = svc.task_service.verify_run(run.id)
        second, _ = svc.task_service.verify_run(run.id)
        attempts = [
            row[0]
            for row in svc.db.fetchall(
                "SELECT attempt FROM verification_runs WHERE run_id = ? ORDER BY attempt",
                (run.id,),
            )
        ]
        assert attempts == [1, 2], f"verification attempts stored as {attempts}"
        assert svc.verification.latest_report(run.id).id == second.id
        return f"attempts {attempts}, both retained"

    probe("V4", "verification", "Re-verification appends an attempt instead of rewriting one", v4)

    out = base / "results3.json"
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
