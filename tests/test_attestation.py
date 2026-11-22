"""MW-P2-03: evidence bundles can carry an optional, honest signature envelope."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile

import pytest

from mayaworkbench.experiments.bundle import (
    sign_run_bundle,
    verify_run_bundle,
    write_run_bundle,
)
from mayaworkbench.experiments.protocol import canonical_sha256
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


# --------------------------------------------------- portable runner bundles
#
# Runner bundles use the same attestation helpers through the bundle module.
# These tests stay at the bundle layer (no service fixtures) so the signing,
# keyed verification and fail-closed behaviour are deterministic.


def _run_bundle(tmp_path, name: str, marker: str):
    payload = {"marker": marker, "seeds": [1]}
    definition = {
        "content_payload": payload,
        "content_sha256": canonical_sha256(payload),
        "kind": "SINGLE",
    }
    target = tmp_path / f"{name}.tar.gz"
    return write_run_bundle(
        target, definition, [], [], {}, {}, {}, "", {}
    ).path


def _run_members(path):
    out: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


def _run_retar(path, members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue(), compresslevel=9, mtime=0))
    return path


def test_run_bundle_sign_then_keyed_verify(tmp_path):
    bundle = _run_bundle(tmp_path, "runner", "alpha")
    assert verify_run_bundle(bundle).ok
    assert "attestation.json" not in _run_members(bundle)

    before = _run_members(bundle)
    assert sign_run_bundle(bundle, key="runner-key") == bundle
    after = _run_members(bundle)
    assert {name: data for name, data in after.items() if name != "attestation.json"} == before
    envelope_text = after["attestation.json"].decode("utf-8")
    assert "runner-key" not in envelope_text
    assert "key_id" in envelope_text

    verified = verify_run_bundle(bundle, key="runner-key")
    assert verified.ok, verified.errors
    assert verified.attestation_present is True
    assert verified.attestation_level == "SIGNED_BY_USER"
    assert verified.attestation_verified is True

    # Without a key the envelope level is reported structurally, but no
    # verification is ever claimed.
    unchecked = verify_run_bundle(bundle)
    assert unchecked.ok, unchecked.errors
    assert unchecked.attestation_level == "SIGNED_BY_USER"
    assert unchecked.attestation_verified is False


def test_run_bundle_wrong_key_fails_verification(tmp_path):
    bundle = _run_bundle(tmp_path, "runner", "alpha")
    sign_run_bundle(bundle, key="runner-key")
    result = verify_run_bundle(bundle, key="wrong-key")
    assert result.ok is False
    assert result.attestation_verified is False
    assert any("attestation verification failed" in item for item in result.errors)


def test_run_bundle_swapped_envelope_and_tampered_manifest_fail(tmp_path):
    first = _run_bundle(tmp_path, "first", "alpha")
    second = _run_bundle(tmp_path, "second", "beta")
    sign_run_bundle(first, key="runner-key")
    envelope = _run_members(first)["attestation.json"]

    swapped_members = _run_members(second)
    swapped_members["attestation.json"] = envelope
    swapped = _run_retar(tmp_path / "swapped.tar.gz", swapped_members)
    swapped_result = verify_run_bundle(swapped, key="runner-key")
    assert swapped_result.ok is False
    assert swapped_result.attestation_verified is False
    assert any("different manifest root" in item for item in swapped_result.errors)

    # The genuine bundle remains verifiable; only the swapped copy failed.
    assert verify_run_bundle(first, key="runner-key").attestation_verified is True

    tampered_members = _run_members(first)
    manifest = json.loads(tampered_members["manifest.json"])
    manifest["root_sha256"] = "f" * 64
    tampered_members["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    tampered = _run_retar(tmp_path / "tampered.tar.gz", tampered_members)
    tampered_result = verify_run_bundle(tampered, key="runner-key")
    assert tampered_result.ok is False
    assert any("root_sha256" in item for item in tampered_result.errors)


def test_sign_run_bundle_fails_closed_on_malformed_archive(tmp_path):
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not a gzip archive")
    with pytest.raises(Exception, match="Cannot sign run bundle"):
        sign_run_bundle(broken, key="runner-key")
