"""Adversarial and unusual-input tests for boundaries that used to be brittle.

Each test describes an input a real repository can contain: a latin-1 log, a
UTF-16 config, a CRLF file, a stale patch, a spoofed path, a closed stdout pipe
or a repository with no commits. The boundary must either handle it or refuse it
with a typed error — never traceback, corrupt the file, or silently pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mayaworkbench.cli.main import main
from mayaworkbench.domain.enums import ToolCallStatus
from mayaworkbench.errors import MayaError
from mayaworkbench.workspace.patch import (
    PatchError,
    _mismatch_message,
    apply_file_patch,
    parse_patch,
)
from mayaworkbench.workspace.policy import PathPolicy, PolicyViolation, normalize_rel
from mayaworkbench.workspace.tools import _dominant_eol

from tests.test_tool_gateway_runner import build_gateway


def patch_for(path: str, old: str, new: str, *, line: int = 1) -> str:
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{line},1 +{line},1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


# -------------------------------------------------------------------- paths
@pytest.mark.parametrize(
    "path",
    [
        "src/bad\x00name.py",
        "src/line\nbreak.py",
        "src/tab\tname.py",
        "src/carriage\rreturn.py",
        "src/\u202egnp.exe",
        "src/\u2066isolate.py",
        "src/line\u2028separator.py",
    ],
)
def test_normalize_rel_refuses_control_and_bidi_characters(path):
    with pytest.raises(PolicyViolation):
        normalize_rel(path)


def test_normalize_rel_refuses_an_over_long_component():
    with pytest.raises(PolicyViolation, match="component"):
        normalize_rel("src/" + "a" * 256)
    # 255 bytes is the filesystem limit and remains legal.
    assert normalize_rel("src/" + "a" * 255).endswith("a" * 255)


def test_path_policy_does_not_follow_a_symlinked_directory(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    policy = PathPolicy(root, allowed_paths=["linked", "src"])
    # Scope alone cannot see the escape; resolution is the read/write boundary
    # and must refuse every symlinked component.
    with pytest.raises(PolicyViolation, match="symlink"):
        policy.existing_file("linked/secret.py")
    with pytest.raises(PolicyViolation, match="symlink"):
        policy.writable_file("linked/new.py")
    assert policy.visible_paths() == []


# ---------------------------------------------------------------- encodings
def test_read_file_labels_lossy_utf8_instead_of_hiding_the_file(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    (workspace / "src" / "latin.py").write_bytes(b"caf\xe9 = 1\n")  # latin-1 é
    result = gateway.invoke("read_file", {"path": "src/latin.py"})
    assert result.status == ToolCallStatus.SUCCEEDED
    assert "caf" in result.output
    assert result.data["encoding"] == "utf-8-replaced"
    assert result.data["replacement_characters"] >= 1
    assert "NOT valid UTF-8" in result.summary


def test_read_file_handles_utf16_text_and_still_refuses_binary(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    (workspace / "src" / "utf16.py").write_bytes("value = 1\n".encode("utf-16"))
    result = gateway.invoke("read_file", {"path": "src/utf16.py"})
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.data["encoding"] == "utf-16"
    assert "value = 1" in result.output

    (workspace / "src" / "blob.bin").write_bytes(b"\x00\x01PNG\x00\x02")
    refused = gateway.invoke("read_file", {"path": "src/blob.bin"})
    assert refused.status == ToolCallStatus.FAILED
    assert "binary" in (refused.error or "")


def test_read_file_refuses_utf32_instead_of_returning_mojibake(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    (workspace / "src" / "wide.py").write_bytes("x = 1\n".encode("utf-32"))
    result = gateway.invoke("read_file", {"path": "src/wide.py"})
    assert result.status == ToolCallStatus.FAILED
    assert "UTF-32" in (result.error or "")


def test_find_symbol_does_not_leak_an_excluded_path(services, indexed, tmp_path):
    hits = [
        {
            "qualified_name": "visible",
            "kind": "function",
            "path": "utils.py",
            "start_line": 1,
            "end_line": 2,
            "signature": "visible()",
            "match_reason": "exact name match",
        },
        {
            "qualified_name": "hidden",
            "kind": "function",
            "path": "src/vendor/hidden.py",
            "start_line": 1,
            "end_line": 2,
            "signature": "hidden()",
            "match_reason": "exact name match",
        },
    ]
    gateway, *_ = build_gateway(
        services,
        indexed,
        tmp_path,
        excluded_paths=["src/vendor"],
        symbol_lookup=lambda _query, _limit: hits,
    )
    result = gateway.invoke("find_symbol", {"query": "visible", "limit": 5})
    assert result.status == ToolCallStatus.SUCCEEDED
    names = [item["qualified_name"] for item in result.data["results"]]
    assert names == ["visible"]
    assert "hidden" not in result.output


def test_write_file_preserves_crlf_and_a_utf8_bom(services, indexed, tmp_path):
    gateway, workspace, *_ = build_gateway(services, indexed, tmp_path)
    target = workspace / "src" / "crlf.py"
    target.write_bytes(b"\xef\xbb\xbfa = 1\r\nb = 2\r\n")
    result = gateway.invoke(
        "write_file", {"path": "src/crlf.py", "content": "a = 10\nb = 2\n", "overwrite": True}
    )
    assert result.status == ToolCallStatus.SUCCEEDED
    assert result.data["eol"] == "crlf"
    assert result.data["bom_preserved"] is True
    assert target.read_bytes() == b"\xef\xbb\xbfa = 10\r\nb = 2\r\n"


def test_a_byte_order_mark_is_decoded_not_treated_as_an_error(services, tmp_path):
    root = tmp_path / "bom-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "d@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "D"], check=True)
    # A Windows-edited Python file: without BOM-aware decoding the U+FEFF reaches
    # ast.parse and the file is reported as a syntax error.
    (root / "bom.py").write_bytes(b"\xef\xbb\xbfdef marker() -> int:\n    return 1\n")
    # UTF-16 text contains NUL bytes; it must not be classified as binary.
    (root / "notes.txt").write_bytes("cafe note\n".encode("utf-16"))
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)

    project = services.create_project("BOM", "byte-order marks")
    attached = services.repository_service.attach(project, root)
    snapshot = services.repository_service.create_snapshot(attached.repository).snapshot
    services.indexer.index(snapshot.id)

    files = {item.path: item for item in services.files.list_by_snapshot(snapshot.id)}
    assert files["bom.py"].parse_coverage.value == "FULL"
    assert "notes.txt" in files
    symbols = services.search.search_symbols(snapshot.id, "marker", limit=5)
    assert any(hit.path == "bom.py" for hit in symbols)
    issues = [
        (issue.kind.value, issue.path)
        for issue in services.issues.list(snapshot.id, limit=100)
    ]
    assert ("PARSE_ERROR", "bom.py") not in issues
    assert ("SKIPPED_BINARY", "notes.txt") not in issues
    assert ("ENCODING_ERROR", "notes.txt") not in issues


def test_dominant_eol_is_conservative_about_mixed_files():
    assert _dominant_eol(b"a\r\nb\r\n") == "\r\n"
    assert _dominant_eol(b"a\rb\r") == "\r"
    assert _dominant_eol(b"a\nb\n") == "\n"
    assert _dominant_eol(b"a\r\nb\n") == "\n"


# ------------------------------------------------------------------- patches
def test_patch_mismatch_names_the_line_and_the_difference():
    target = b"first = 1\nsecond = 2\nthird = 3\n"
    file_patch = parse_patch(patch_for("m.py", "second = 99", "second = 20", line=2))[0]
    with pytest.raises(PatchError) as excinfo:
        apply_file_patch(target, file_patch)
    message = str(excinfo.value)
    assert "line 2" in message
    assert "second = 99" in message and "second = 2" in message
    assert "re-read the current file" in message


def test_patch_mismatch_explains_a_crlf_mismatch():
    target = b"first = 1\r\nsecond = 2\r\n"
    file_patch = parse_patch(patch_for("m.py", "second = 2", "second = 3", line=2))[0]
    with pytest.raises(PatchError) as excinfo:
        apply_file_patch(target, file_patch)
    assert "CRLF" in str(excinfo.value)


def test_mismatch_message_flags_whitespace_only_differences():
    message = _mismatch_message(
        "m.py", 0, ("value = 1  \n",), ("value = 1\n",)
    )
    assert "whitespace" in message


def test_patch_cannot_smuggle_a_null_byte_into_a_text_file():
    with pytest.raises(PatchError, match="null byte"):
        apply_file_patch(b"x = 1\n", parse_patch(patch_for("m.py", "x = 1", "x = \x001"))[0])


def test_patch_refuses_a_non_utf8_target_explicitly():
    with pytest.raises(PatchError, match="not valid UTF-8"):
        apply_file_patch(b"caf\xe9 = 1\n", parse_patch(patch_for("m.py", "café = 1", "café = 2"))[0])


# ----------------------------------------------------------------------- CLI
class _BrokenPipe:
    def write(self, *_args, **_kwargs):
        raise BrokenPipeError()

    def flush(self):
        return None

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("this stream has no file descriptor")


def test_a_closed_stdout_pipe_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", _BrokenPipe())
    code = main(["--home", str(tmp_path / "home"), "--no-color", "project", "list"])
    assert code == 0


def test_json_errors_stay_machine_readable_on_a_missing_project(tmp_path, capsys):
    code = main(["--home", str(tmp_path / "home"), "--json", "project", "show", "nope"])
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["code"] == "NOT_FOUND"
    assert payload["error"]


# ----------------------------------------------------------------- git edges
def test_a_repository_without_commits_fails_with_a_typed_error(services, tmp_path):
    root = tmp_path / "empty-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    project = services.create_project("Empty", "no commits yet")
    attached = services.repository_service.attach(project, root)
    with pytest.raises(MayaError) as excinfo:
        services.repository_service.create_snapshot(attached.repository)
    assert excinfo.value.code
    assert "commit" in str(excinfo.value).lower()
