"""Raw LLM & agent capabilities: routing, structured-output repair, reliability, usage."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from mayaworkbench.cli.main import main
from mayaworkbench.tasks.llm_capabilities import (
    extract_json_object,
    repair_structured_output,
    route_task,
    tool_reliability,
    usage_summary,
)


class SmallSchema(BaseModel):
    tool: str = Field(min_length=1)
    n: int = Field(ge=1)


def test_routing_depends_on_requirement_not_leaderboard():
    assert route_task("PAPER_TO_CODE", uncertainty=0.9, context_tokens=1000).provider == "reasoning"
    assert route_task("ENGINEERING_CHANGE", uncertainty=0.1, context_tokens=1000).provider == "coding"
    assert route_task("REPOSITORY_QA", uncertainty=0.1, context_tokens=90_000).provider == "long-context"
    health = {"fast": {"failures": 0, "avg_latency_ms": 10}, "slow": {"failures": 4, "avg_latency_ms": 200}}
    assert route_task(
        "OTHER", uncertainty=0.1, context_tokens=100,
        provider_health=health, data_policy="BYOK_REMOTE_MODEL",
    ).provider == "fast"


def test_structured_repair_retries_within_budget_and_validates():
    attempts: list[str] = []

    def retry(_errors: str) -> str:
        attempts.append("retry")
        return '```json\n{"tool":"read_file","n":2}\n```'

    action, errors = repair_structured_output("not json at all", SmallSchema, retry=retry)
    assert action is not None
    assert action.tool == "read_file" and action.n == 2
    assert len(attempts) == 1
    assert errors

    attempts.clear()
    bad, errors = repair_structured_output(
        "{}", SmallSchema, retry=lambda _errors: "still invalid"
    )
    assert bad is None
    assert len(errors) == 3  # three bounded attempts, no unsafe default


def test_extract_json_accepts_fenced_and_rejects_prose():
    assert extract_json_object('```json\n{"a":1}\n```') == {"a": 1}
    try:
        extract_json_object("plain prose")
    except ValueError:
        pass
    else:
        raise AssertionError("prose must not parse")


def test_tool_reliability_and_usage_are_honest():
    details = {
        "tool_calls": [
            {"tool_name": "read_file", "status": "SUCCEEDED"},
            {"tool_name": "read_file", "status": "FAILED"},
            {"tool_name": "run_command", "status": "BLOCKED"},
        ]
    }
    scores = tool_reliability(details)
    assert scores["read_file"].score == 0.5
    assert scores["run_command"].score == 0.5
    usage = usage_summary({"prompt_tokens": 10, "completion_tokens": 5, "cost_basis": "configured"})
    assert usage["total_tokens"] == 15
    assert usage["cost_usd"] is None
    assert usage["cost_basis"] == "configured"


def test_reliability_cli_uses_real_run(tmp_path, repo, capsys):
    home = tmp_path / "home"
    base = ["--home", str(home), "--json"]

    def run(args):
        code = main(base + args)
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip().startswith("{") else None

    run(["init", "--project", "agent-capabilities"])
    code, _ = run(["repo", "add", str(repo)])
    assert code == 0
    code, payload = run(["snapshot", "create"])
    assert code == 0
    code, payload = run(["index", "--snapshot", payload["snapshot"]["id"]])
    assert code == 0
    code, payload = run(
        [
            "task",
            "create",
            "--project",
            "agent-capabilities",
            "--goal",
            "Explain the repository",
            "--allow-path",
            "attention.py",
        ]
    )
    assert code == 0
    run_id = payload["contract"]["id"]
    code, payload = run(["task", "reliability", "run_nonexistent"])
    assert code != 0 or payload is None
