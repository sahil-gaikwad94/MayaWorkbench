from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mayaworkbench.cli.main import main
from mayaworkbench.config import AppPaths, load_settings, save_settings

PLAN = {
    "summary": "Change count_tokens and check it.",
    "steps": [
        {
            "description": "Inspect and change utils.py.",
            "files": ["utils.py"],
            "checks": ["python -c ..."],
        }
    ],
    "files": ["utils.py"],
    "checks": [],
    "risks": [],
    "unknowns": [],
    "estimated_steps": 4,
}


def actions():
    return [
        {
            "thought": "Read the target.",
            "tool": "read_file",
            "arguments": {"path": "utils.py"},
        },
        {
            "thought": "Make the minimal change.",
            "tool": "apply_patch",
            "arguments": {
                "patch": (
                    "--- a/utils.py\n"
                    "+++ b/utils.py\n"
                    "@@ -8,3 +8,3 @@\n"
                    " def count_tokens(text: str) -> int:\n"
                    '     """Count whitespace-delimited tokens."""\n'
                    "-    return len(TOKEN_PATTERN.findall(text))\n"
                    "+    return len(text.split())\n"
                )
            },
        },
        {
            "thought": "Check behavior.",
            "tool": "run_command",
            "arguments": {
                "argv": [
                    "python",
                    "-c",
                    "from utils import count_tokens; assert count_tokens('a b') == 2; print('ok')",
                ],
                "timeout_seconds": 30,
            },
        },
        {
            "thought": "Finish.",
            "tool": "finish",
            "arguments": {
                "summary": "Changed count_tokens and ran a bounded check.",
                "limitations": ["No full suite was run."],
                "checks_run": ["python -c"],
                "remaining_work": [],
            },
        },
    ]


class MockModel:
    def __init__(self):
        self.actions = actions()
        self.requests = 0
        self.server = None

    def handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # noqa: N802
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                mock.requests += 1
                schema = (
                    (body.get("response_format") or {})
                    .get("json_schema", {})
                    .get("name")
                )
                if schema == "PlanPayload":
                    payload = PLAN
                elif schema == "AgentAction":
                    if not mock.actions:
                        self.send_error(500, "no actions")
                        return
                    payload = mock.actions.pop(0)
                else:
                    self.send_error(400, f"unexpected schema {schema}")
                    return
                content = json.dumps(payload)
                data = {
                    "id": "mock",
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 50,
                        "total_tokens": 100,
                    },
                }
                raw = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()


def run(capsys, args):
    code = main(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else None
    return code, payload, captured


def test_cli_full_flow_uses_a_separate_worker_process(
    capsys, home: Path, repo: Path, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("MAYA_MODEL_API_KEY", "test-key")
    code, _, _ = run(capsys, ["--home", str(home), "--json", "init", "--project", "p3", "--slug", "p3"])
    assert code == 0
    run(capsys, ["--home", str(home), "--json", "repo", "add", str(repo), "--project", "p3"])
    code, snapshot, _ = run(
        capsys, ["--home", str(home), "--json", "snapshot", "create", "--project", "p3"]
    )
    assert code == 0
    snapshot_id = snapshot["snapshot"]["id"]
    code, _, _ = run(
        capsys, ["--home", str(home), "--json", "index", "--project", "p3", "--snapshot", snapshot_id]
    )
    assert code == 0

    with MockModel() as mock:
        paths = AppPaths.resolve(str(home))
        settings = load_settings(paths)
        settings = settings.model_copy(
            update={
                "data_policy": "BYOK_REMOTE_MODEL",
                "model": settings.model.model_copy(
                    update={
                        "provider": "openai_compatible",
                        "model": "mock-1",
                        "base_url": f"http://127.0.0.1:{mock.server.server_address[1]}/v1",
                        "api_key_env": "MAYA_MODEL_API_KEY",
                        # The local mock states a zero price; an unknown price is
                        # refused by the budget controller.
                        "input_cost_per_mtok": 0.0,
                        "output_cost_per_mtok": 0.0,
                    }
                ),
            }
        )
        save_settings(paths, settings)
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(PLAN), encoding="utf-8")

        code, created, captured = run(
            capsys,
            [
                "--home",
                str(home),
                "--json",
                "task",
                "create",
                "--project",
                "p3",
                "--goal",
                "Change count_tokens and check it",
                "--allow-path",
                "utils.py",
                "--max-steps",
                "8",
                "--max-tool-calls",
                "20",
                "--network",
                "ALLOW_WITH_APPROVAL",
                "--check",
                "python -c \"from utils import count_tokens; assert count_tokens('a b') == 2; print('ok')\"",
                "--check-criteria",
                "AC1",
            ],
        )
        assert code == 0, captured.err
        task_id = created["task"]["id"]
        code, started, captured = run(
            capsys,
            [
                "--home",
                str(home),
                "--json",
                "task",
                "start",
                task_id,
                "--plan-file",
                str(plan_path),
            ],
        )
        assert code == 0, captured.err
        run_id = started["run"]["id"]
        for approval in started["approvals"]:
            code, _, _ = run(
                capsys,
                ["--home", str(home), "--json", "task", "approve", approval["id"]],
            )
            assert code == 0

        code, finished, captured = run(
            capsys,
            ["--home", str(home), "--json", "task", "run", task_id, "--timeout", "90"],
        )
        assert code == 0, captured.err
        assert finished["run"]["state"] == "REVIEWING", captured.err
        assert finished["run"]["tool_call_count"] == 3
        assert (repo / "utils.py").read_text().count("TOKEN_PATTERN.findall") == 1
        diff = run(capsys, ["--home", str(home), "--json", "task", "diff", run_id])[1]
        assert "+    return len(text.split())" in diff["patch"]
        review = run(capsys, ["--home", str(home), "--json", "task", "review", run_id])[1]
        assert review["review"]["scope_ok"] is True
        code, verified, captured = run(
            capsys,
            ["--home", str(home), "--json", "verify", "run", run_id, "--no-review"],
        )
        assert code == 0, captured.err
        assert verified["report"]["decision"] in {
            "ACCEPTED",
            "COMPLETED_WITH_LIMITATIONS",
        }, verified["report"]["blocking"]
        bundle_dir = tmp_path / "evidence-bundle"
        code, bundled, captured = run(
            capsys,
            ["--home", str(home), "--json", "verify", "bundle", run_id, str(bundle_dir)],
        )
        assert code == 0, captured.err
        code, checked, captured = run(
            capsys,
            ["--home", str(home), "--json", "verify", "check-bundle", str(bundle_dir)],
        )
        assert code == 0, captured.err
        assert checked["ok"] is True
        code, decided, captured = run(
            capsys,
            [
                "--home",
                str(home),
                "--json",
                "task",
                "finish",
                run_id,
                "--accept",
                "--reason",
                "reviewed",
            ],
        )
        assert code == 0, captured.err
        assert decided["run"]["state"] == "COMPLETED_WITH_LIMITATIONS"
        assert mock.requests == 4
