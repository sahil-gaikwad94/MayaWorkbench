"""Optional signature envelope for evidence bundles (MW-P2-03).

A local hash detects accidental modification; it cannot distinguish a bundle
whose manifest was rewritten by an attacker. An HMAC over the manifest root
hash proves which held key signed that root. It does **not** prove that the
scientific result is true, and it is optional: unsigned bundles stay usable and
are labelled ``UNATTESTED``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import VerificationError

ATTESTATION_NAME = "attestation.json"
PROOF_NAME = ".maya-proof.json"

#: Attestation levels are deliberately about *who signed*, never about result truth.
ATTESTED_LEVELS = ("UNATTESTED", "HASHED", "SIGNED_BY_USER", "SIGNED_BY_CI")


def _load_key(key: bytes | str | None) -> bytes:
    if key is not None:
        return key.encode("utf-8") if isinstance(key, str) else key
    raw = os.environ.get("MAYA_SIGNING_KEY", "")
    if not raw:
        raise VerificationError(
            "No signing key is available",
            hint="Set MAYA_SIGNING_KEY in your environment; it is never stored in the bundle.",
        )
    return raw.encode("utf-8")


def _root_hash(entries: list[dict[str, Any]]) -> str:
    lines = "\n".join(
        f"{entry['sha256']}  {entry['name']}"
        for entry in sorted(entries, key=lambda item: item["name"])
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def sign_bundle(directory: str | Path, *, key: bytes | str | None = None) -> dict[str, Any]:
    target = Path(directory).expanduser().resolve()
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise VerificationError("manifest.json is missing; nothing to sign")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise VerificationError("manifest has no file list")
    root = manifest.get("root_sha256") or _root_hash(entries)
    secret = _load_key(key)
    signature = hmac.new(secret, root.encode("ascii"), hashlib.sha256).hexdigest()
    payload = {
        "level": "SIGNED_BY_USER",
        "key_id": hashlib.sha256(secret).hexdigest()[:16],
        "root_sha256": root,
        "signature": signature,
        "algorithm": "HMAC-SHA256",
        "signed_at": utcnow().isoformat(),
        "claim": "This signature identifies the signing key for this manifest root; "
        "it does not prove the underlying scientific result.",
    }
    (target / ATTESTATION_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return payload


def verify_attestation(
    directory: str | Path, *, key: bytes | str | None = None
) -> dict[str, Any]:
    target = Path(directory).expanduser().resolve()
    path = target / ATTESTATION_NAME
    if not path.is_file():
        return {"level": "UNATTESTED", "ok": True, "verified": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"level": "UNATTESTED", "ok": False, "verified": False, "error": str(exc)}
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        return {"level": "UNATTESTED", "ok": False, "verified": False, "error": "manifest missing"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_root = manifest.get("root_sha256") or _root_hash(manifest.get("files", []))
    if payload.get("root_sha256") != current_root:
        return {
            "level": "SIGNED_BY_USER",
            "ok": False,
            "verified": False,
            "error": "attestation refers to a different manifest root",
        }
    if key is None:
        return {
            "level": payload.get("level", "SIGNED_BY_USER"),
            "ok": True,
            "verified": False,
            "key_id": payload.get("key_id"),
            "note": "signature present but no key supplied for verification",
        }
    secret = _load_key(key)
    expected = hmac.new(secret, current_root.encode("ascii"), hashlib.sha256).hexdigest()
    matches = hmac.compare_digest(expected, str(payload.get("signature", "")))
    return {
        "level": payload.get("level", "SIGNED_BY_USER"),
        "ok": matches,
        "verified": matches,
        "key_id": payload.get("key_id"),
        "error": None if matches else "signature does not match the supplied key",
    }


def write_proof(
    directory: str | Path,
    *,
    level: str = "SIGNED_BY_USER",
    identity: str | None = None,
    key: bytes | str | None = None,
) -> dict[str, Any]:
    """Write a `.maya-proof.json` envelope identifying the signer of a manifest root.

    ``SIGNED_BY_CI`` requires an explicit CI identity from the environment; the
    signature still proves only which key signed, not that the result is true.
    """
    if level not in ATTESTED_LEVELS:
        raise VerificationError(f"Unknown attestation level: {level}")
    if level == "SIGNED_BY_CI" and not identity:
        identity = os.environ.get("MAYA_CI_IDENTITY", "")
        if not identity:
            raise VerificationError(
                "SIGNED_BY_CI requires an identity",
                hint="Set MAYA_CI_IDENTITY in the signing environment.",
            )
    target = Path(directory).expanduser().resolve()
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise VerificationError("manifest.json is missing; nothing to attest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest.get("root_sha256") or _root_hash(manifest.get("files", []))
    secret = _load_key(key)
    signature = hmac.new(secret, root.encode("ascii"), hashlib.sha256).hexdigest()
    payload = {
        "schema": "maya-proof/v1",
        "level": level,
        "identity": identity or "local-user",
        "key_id": hashlib.sha256(secret).hexdigest()[:16],
        "root_sha256": root,
        "signature": signature,
        "algorithm": "HMAC-SHA256",
        "attested_at": utcnow().isoformat(),
        "claim": (
            "This signature identifies the signing key and identity for this manifest "
            "root. It does not prove the scientific result."
        ),
    }
    (target / PROOF_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return payload


def verify_proof(directory: str | Path, *, key: bytes | str | None = None) -> dict[str, Any]:
    """Verify a proof envelope against the current manifest root and held key."""
    target = Path(directory).expanduser().resolve()
    path = target / PROOF_NAME
    if not path.is_file():
        return {"ok": True, "level": "UNATTESTED", "verified": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "level": "UNATTESTED", "verified": False, "error": str(exc)}
    if payload.get("schema") != "maya-proof/v1":
        return {"ok": False, "level": "UNATTESTED", "verified": False, "error": "unknown proof schema"}
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "level": "UNATTESTED", "verified": False, "error": "manifest missing"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest.get("root_sha256") or _root_hash(manifest.get("files", []))
    if payload.get("root_sha256") != root:
        return {
            "ok": False,
            "level": payload.get("level", "UNATTESTED"),
            "verified": False,
            "error": "proof refers to a different manifest root",
        }
    if key is None:
        return {
            "ok": True,
            "level": payload.get("level", "UNATTESTED"),
            "verified": False,
            "identity": payload.get("identity"),
            "note": "no key supplied; signature was not verified",
        }
    secret = _load_key(key)
    expected = hmac.new(secret, root.encode("ascii"), hashlib.sha256).hexdigest()
    matches = hmac.compare_digest(expected, str(payload.get("signature", "")))
    return {
        "ok": matches,
        "level": payload.get("level", "UNATTESTED"),
        "verified": matches,
        "identity": payload.get("identity"),
        "error": None if matches else "signature does not match the supplied key",
    }


__all__ = [
    "ATTESTATION_NAME",
    "PROOF_NAME",
    "ATTESTED_LEVELS",
    "sign_bundle",
    "verify_attestation",
    "write_proof",
    "verify_proof",
]
