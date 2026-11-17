from __future__ import annotations

import os
from pathlib import Path

import pytest

from mayaworkbench.workspace.patch import PatchError, apply_patch, parse_patch
from mayaworkbench.workspace.policy import (
    PathPolicy,
    PolicyViolation,
    contains_secret_material,
    is_secret_path,
    normalize_rel,
)


def test_normalize_rejects_traversal_absolute_and_null():
    assert normalize_rel("src/a.py") == "src/a.py"
    for value in ("../secret", "/etc/passwd", "src/../x", "src//x", ".git/config", "src/\x00x"):
        with pytest.raises(PolicyViolation):
            normalize_rel(value)


def test_policy_scope_exclusion_and_secret(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "src" / "secret").mkdir()
    (root / "src" / "secret" / "key.txt").write_text("nope\n")
    (root / ".env").write_text("TOKEN=1\n")
    policy = PathPolicy(root, allowed_paths=["src"], excluded_paths=["src/secret"])
    assert policy.check_write("src/a.py") == "src/a.py"
    with pytest.raises(PolicyViolation):
        policy.check_write("other.py")
    with pytest.raises(PolicyViolation):
        policy.check_write("src/secret/key.txt")
    with pytest.raises(PolicyViolation):
        policy.existing_file(".env")
    with pytest.raises(PolicyViolation):
        policy.writable_file("src/secret/key.txt")
    visible = policy.visible_paths()
    assert "src/a.py" in visible
    assert "src/secret/key.txt" not in visible
    assert ".env" not in visible


def test_policy_refuses_symlink_components(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("secret\n")
    os.symlink(outside, root / "link")
    policy = PathPolicy(root, allowed_paths=["link"])
    with pytest.raises(PolicyViolation):
        policy.existing_file("link/passwd")
    with pytest.raises(PolicyViolation):
        policy.writable_file("link/passwd")


def test_policy_deletion_flag_and_secret_helpers(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("x\n")
    policy = PathPolicy(root, allowed_paths=["a.txt"])
    with pytest.raises(PolicyViolation):
        policy.deletion_allowed("a.txt")
    allowed = PathPolicy(root, allowed_paths=["a.txt"], allow_file_deletion=True)
    assert allowed.deletion_allowed("a.txt") is True
    assert is_secret_path(".env.local")
    assert is_secret_path("certs/server.pem")
    assert not is_secret_path("src/config.py")
    assert contains_secret_material("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    assert not contains_secret_material("def add(a, b): return a + b")


MODIFY = """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+three
"""

CREATE = """--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+x
+y
"""

DELETE = """--- a/a.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""


def test_apply_modify_create_delete():
    result = apply_patch(MODIFY, {"a.txt": b"one\ntwo\n"})
    assert result[0].data == b"one\nthree\n"
    assert not result[0].created and not result[0].deleted
    result = apply_patch(CREATE, {})
    assert result[0].created and result[0].data == b"x\ny\n"
    result = apply_patch(DELETE, {"a.txt": b"one\ntwo\n"})
    assert result[0].deleted and result[0].data == b""


def test_patch_refuses_fuzz_stale_and_unsafe_features():
    with pytest.raises(PatchError, match="does not match"):
        apply_patch(MODIFY, {"a.txt": b"one\nTWO\n"})
    with pytest.raises(PatchError, match="does not match"):
        apply_patch(MODIFY, {"a.txt": b"zero\none\ntwo\n"})
    with pytest.raises(PatchError, match="renames"):
        parse_patch("rename from a\nrename to b\n")
    with pytest.raises(PatchError, match="binary"):
        parse_patch("Binary files a/x and b/x differ\n")
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(CREATE, {"new.txt": b"present\n"})
    with pytest.raises(PatchError, match="does not exist"):
        apply_patch(MODIFY, {})


def test_patch_rejects_overlap_and_bad_counts():
    overlap = """--- a/a.txt
+++ b/a.txt
@@ -1,1 +1,1 @@
-a
+A
@@ -1,1 +1,1 @@
-a
+B
"""
    with pytest.raises(PatchError, match="overlapping"):
        apply_patch(overlap, {"a.txt": b"a\n"})
    bad = """--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,1 @@
-a
+A
"""
    with pytest.raises(PatchError, match="declares 3 old lines"):
        parse_patch(bad)


def test_patch_no_newline_marker_and_empty_file():
    patch = """--- a/a.txt
+++ b/a.txt
@@ -1,1 +1,1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    result = apply_patch(patch, {"a.txt": b"old"})
    assert result[0].data == b"new"
    empty = """--- /dev/null
+++ b/empty.txt
"""
    assert apply_patch(empty, {})[0].data == b""
