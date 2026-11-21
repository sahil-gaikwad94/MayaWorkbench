"""MW-P2-03: evidence bundles can carry an optional, honest signature envelope."""

from __future__ import annotations

import hashlib
import json

import pytest

from mayaworkbench.verification.attestation import (
    sign_bundle,
    verify_attestation,
    verify_proof,
    write_proof,
)
from mayaworkbench.verification.bundle import verify_bundle, write_bundle


def _bundle(tmp_path):
    patch = b"--- a/x\n+++ b/x\n"
    payloads = {
        "report.json": json.dumps(
            {
                "decision": "ACCEPTED",
                "criteria": [],
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
            }
        ).encode(),
        "report.md": b"# Evidence report\n",
        "patch.diff": patch,
        "checks.json": b"[]",
        "criteria.json": b"[]",
    }
    return write_bundle(tmp_path / "bundle", payloads).path


def test_unsigned_bundle_is_unattested(tmp_path):
    target = _bundle(tmp_path)
    attestation = verify_attestation(target)
    assert attestation["level"] == "UNATTESTED"
    assert verify_bundle(target).ok is True


def test_signed_bundle_verifies_with_the_matching_key(tmp_path):
    target = _bundle(tmp_path)
    signed = sign_bundle(target, key="correct-key")
    assert signed["level"] == "SIGNED_BY_USER"
    assert len(signed["signature"]) == 64
    report = verify_bundle(target)
    assert report.ok is True
    assert report.attestation["level"] == "SIGNED_BY_USER"

    verified = verify_attestation(target, key="correct-key")
    assert verified["verified"] is True
    assert verified["key_id"] == signed["key_id"]


def test_wrong_key_fails_closed(tmp_path):
    target = _bundle(tmp_path)
    sign_bundle(target, key="correct-key")
    result = verify_attestation(target, key="wrong-key")
    assert result["ok"] is False
    assert result["verified"] is False


def test_maya_proof_envelope_records_signer_identity(tmp_path, monkeypatch):
    target = _bundle(tmp_path)
    monkeypatch.setenv("MAYA_SIGNING_KEY", "proof-key")
    monkeypatch.setenv("MAYA_CI_IDENTITY", "ci-runner-42")
    proof = write_proof(target, level="SIGNED_BY_CI")
    assert proof["level"] == "SIGNED_BY_CI"
    assert proof["identity"] == "ci-runner-42"
    result = verify_proof(target, key="proof-key")
    assert result["ok"] is True and result["verified"] is True
    assert result["identity"] == "ci-runner-42"


def test_maya_proof_detects_manifest_tampering(tmp_path):
    target = _bundle(tmp_path)
    write_proof(target, key="proof-key")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["root_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest))
    result = verify_proof(target, key="proof-key")
    assert result["ok"] is False
    assert "different manifest root" in result["error"]


def test_ci_proof_requires_identity(tmp_path, monkeypatch):
    target = _bundle(tmp_path)
    monkeypatch.setenv("MAYA_SIGNING_KEY", "proof-key")
    monkeypatch.delenv("MAYA_CI_IDENTITY", raising=False)
    with pytest.raises(Exception):
        write_proof(target, level="SIGNED_BY_CI")


def test_tampered_manifest_root_is_detected(tmp_path):
    target = _bundle(tmp_path)
    sign_bundle(target, key="correct-key")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["root_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    result = verify_attestation(target, key="correct-key")
    assert result["ok"] is False
    assert "different manifest root" in result["error"]
    assert verify_bundle(target).ok is False
