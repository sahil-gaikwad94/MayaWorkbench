# Spec — signed portable run bundles (WP RUNBUNDLE-1)

**Date:** 2026-11-22
**Status:** SHIPPED (2026-11-22) — sign/verify/import with named key; HMAC verified at verify time; no CI identity claim.
**Vision source:** Audit A WP5; Audit B Upgrade 1; v2.1 §7 reproducibility.

## Problem

Portable run bundles already use deterministic archives with per-file SHA-256,
root-hash verification and an optional HMAC attestation envelope. The missing
piece is operational: there is no CLI path to sign a bundle, no key plumbing on
import, and `verify_run_bundle` only checks that an attestation envelope has a
signature string — it does not verify the signature. Tampering with the envelope
is therefore detected only at import, and only with a key in the environment.

## Product outcome

A user can sign, verify and import a portable run bundle through explicit CLI
flags with a named key source; verify failures are reported at verify time, and
imported results remain honestly labelled `IMPORTED`/`UNATTESTED` unless a
trusted signature verifies. No claim that a signature proves a scientific result.

## Scope (in)

- `experiments.bundle.sign_run_bundle(path, *, key=None) -> Path` — stage archive,
  use the existing `verification.attestation.sign_bundle`, rewrite archive with
  `attestation.json`; fail closed on malformed archives.
- `verify_run_bundle(path, *, key=None)` — optional keyed verification of the
  envelope using the existing HMAC helper; structured `attestation_level` and
  `attestation_verified` on `RunBundleVerification`; a wrong-key/tampered
  envelope is a verification error.
- Thread `key` through `ExperimentService.import_run_bundle` and CLI
  `maya runner import --verify-with <ENV_NAME>` / `--key` resolution (value from
  environment only; never from a literal argument).
- `maya runner export --sign [--signing-key-env NAME]`.
- Tests for sign→verify→import, wrong-key failure, swapped-root envelope failure,
  and archive byte hash binding.

## Explicitly out

- CI identity issuance, new crypto primitives, trusting any key by default.
- Claiming that a signature proves numerical reproducibility.

## Design constraints

- Reuse the existing HMAC attestation helper and `MAYA_SIGNING_KEY` default.
- Never print or persist key material; record only `key_id`.
- Attestation envelope remains exempt from the manifest root hash but must be
  bound to it via the HMAC; a signature from another root must fail.
- Verification remains mandatory before import.

## Acceptance criteria

1. `sign_run_bundle` produces an archive that `verify_run_bundle(key=...)` reports
   `attestation_verified=True`.
2. A wrong key, a swapped envelope from another bundle, or a tampered manifest
   makes verification fail with a typed error.
3. CLI sign/import flags work end to end and never echo key values.
4. Full suite green; status docs updated.
