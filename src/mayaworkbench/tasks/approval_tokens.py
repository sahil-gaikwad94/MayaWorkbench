"""Optional HMAC integrity for approval artifacts (v2.1 REFINE, MW-P2).

An HMAC protects the *artifact* from tampering in transit or at rest. It is not
an authority: the server-side one-time consume, scope hash, expiry and replay
protection remain the actual authorization. If no key is configured the approval
works exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

KEY_ENV = "MAYA_APPROVAL_KEY"


def _message(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _key() -> bytes | None:
    raw = os.environ.get(KEY_ENV, "")
    return raw.encode("utf-8") if raw else None


def approval_message(*, approval_id: str, spec_sha256: str, nonce: str, expires_at: str, action: str) -> dict[str, Any]:
    return {
        "approval_id": approval_id,
        "spec_sha256": spec_sha256,
        "nonce": nonce,
        "expires_at": expires_at,
        "action": action,
    }


def sign_approval(message: dict[str, Any]) -> dict[str, str] | None:
    key = _key()
    if key is None:
        return None
    return {
        "algorithm": "HMAC-SHA256",
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "signature": hmac.new(key, _message(message), hashlib.sha256).hexdigest(),
    }


def verify_approval(message: dict[str, Any], envelope: dict[str, Any] | None) -> bool:
    if envelope is None:
        return True  # no signing configured for this approval
    key = _key()
    if key is None:
        return False  # signed artifact but key is gone: fail closed
    expected = hmac.new(key, _message(message), hashlib.sha256).hexdigest()
    return (
        envelope.get("algorithm") == "HMAC-SHA256"
        and hmac.compare_digest(str(envelope.get("signature", "")), expected)
    )


__all__ = ["KEY_ENV", "approval_message", "sign_approval", "verify_approval"]
