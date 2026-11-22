"""The optional LSP adapter must be fail-closed and prove its provenance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from mayaworkbench.indexing.lsp_client import (
    SERVER_PROFILES,
    LspClient,
    LspError,
    LspQuery,
    _Profile,
    lsp_adapter_for,
    lsp_unsupported_reason,
)

_FAKE_SERVER = r'''#!__PYTHON__
import json, sys
def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line: return None
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":",1)[1].strip()); break
    if length is None: return None
    sys.stdin.buffer.readline()  # blank separator after headers
    return json.loads(sys.stdin.buffer.read(length))
def send(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()
while True:
    message = read()
    if message is None: break
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"serverInfo":{"name":"fake-lsp","version":"1.2.3"}}})
    elif method == "textDocument/definition":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"uri":"file:///work/utils.py","range":{"start":{"line":7,"character":0},"end":{"line":7,"character":4}}}})
    elif method == "textDocument/references":
        send({"jsonrpc":"2.0","id":message["id"],"result":[{"uri":"file:///work/utils.py","range":{"start":{"line":0,"character":0},"end":{"line":0,"character":2}}}]})
    elif method == "textDocument/hover":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"contents":{"value":"causal mask"}}})
    elif method == "shutdown":
        send({"jsonrpc":"2.0","id":message["id"],"result":None})
    elif method == "exit":
        break
'''

_FAKE_SERVER = _FAKE_SERVER.replace("__PYTHON__", sys.executable)

_BAD_SERVER = "#!" + sys.executable + "\nimport sys\nsys.exit(3)\n"


def _write_server(path: Path, name: str, source: str) -> Path:
    script = path / name
    script.write_text(source, encoding="utf-8")
    script.chmod(0o700)
    return script


def _client(script: Path, root: Path) -> LspClient:
    binary, argv = SERVER_PROFILES["python"]
    return LspClient(_Profile(binary, argv, str(script)), root, "snap_01TEST", opener=subprocess.Popen)


def test_disabled_factory_returns_none_without_spawning(tmp_path, monkeypatch):
    monkeypatch.delenv("MAYA_LSP_ENABLE", raising=False)
    assert "disabled" in (lsp_unsupported_reason("python") or "")
    assert lsp_adapter_for("python", snapshot_path=tmp_path) is None


def test_unknown_language_and_missing_binary_are_unsupported(tmp_path, monkeypatch):
    monkeypatch.setenv("MAYA_LSP_ENABLE", "1")
    assert "no allowlisted" in (lsp_unsupported_reason("cobol") or "")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert "not on PATH" in (lsp_unsupported_reason("python") or "")
    assert lsp_adapter_for("python", snapshot_path=tmp_path) is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("LSP_DEFINITION", 1), ("LSP_REFERENCE", 1), ("LSP_HOVER", 0)],
)
def test_lsp_round_trips_with_provenance(tmp_path, kind, expected):
    script = _write_server(tmp_path, "fake-lsp", _FAKE_SERVER)
    (tmp_path / "utils.py").write_text("x = 1\n", encoding="utf-8")
    client = _client(script, tmp_path)
    try:
        result = client.query(
            LspQuery(
                kind=kind, path="utils.py", line=1, column=1,
                language="python", snapshot_id="snap_01TEST",
            )
        )
        assert result.confidence == "HIGH"
        assert result.provenance == kind
        assert result.server == "pyright-langserver"
        assert result.server_version == "1.2.3"
        assert len(result.targets) == expected
        if kind == "LSP_HOVER":
            assert result.hover == "causal mask"
    finally:
        client.close()
    assert client._proc is None


def test_crashing_server_fails_closed(tmp_path):
    script = _write_server(tmp_path, "bad-lsp", _BAD_SERVER)
    (tmp_path / "utils.py").write_text("x = 1\n", encoding="utf-8")
    client = _client(script, tmp_path)
    try:
        with pytest.raises(LspError):
            client.query(
                LspQuery(kind="LSP_DEFINITION", path="utils.py", line=1, column=1, language="python")
            )
    finally:
        client.close()


def test_child_environment_is_sanitized(tmp_path, monkeypatch):
    probe = _write_server(
        tmp_path,
        "env-lsp",
        "#!" + sys.executable + "\n"
        + "import json, sys, os\n"
        + "while True:\n"
        + "    line=sys.stdin.buffer.readline()\n"
        + "    if not line: break\n"
        + "    if not line.lower().startswith(b'content-length:'): continue\n"
        + "    length=int(line.split(b':',1)[1])\n"
        + "    sys.stdin.buffer.readline()\n"
        + "    msg=json.loads(sys.stdin.buffer.read(length))\n"
        + "    marker='LEAK' if os.environ.get('MAYA_LSP_SECRET_CANARY') else 'CLEAN'\n"
        + "    data=json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'uri':'file:///work/'+marker}}).encode()\n"
        + "    sys.stdout.buffer.write(b'Content-Length: '+str(len(data)).encode()+b'\\r\\n\\r\\n'+data)\n"
        + "    sys.stdout.buffer.flush()\n",
    )
    monkeypatch.setenv("MAYA_LSP_SECRET_CANARY", "canary-must-not-leak")
    (tmp_path / "utils.py").write_text("x = 1\n", encoding="utf-8")
    client = LspClient(
        _Profile("env-lsp", (), str(probe)), tmp_path, opener=subprocess.Popen
    )
    try:
        result = client.query(
            LspQuery(kind="LSP_DEFINITION", path="utils.py", line=1, column=1, language="python")
        )
        assert result.targets[0]["path"] == "CLEAN"
    finally:
        client.close()


def test_path_traversal_is_refused_at_validation(tmp_path):
    with pytest.raises(ValidationError):
        LspQuery(kind="LSP_DEFINITION", path="../outside.py", line=1, column=1, language="python")
