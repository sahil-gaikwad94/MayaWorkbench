"""CLI commands for user-owned portable runner bundles (WP10).

The parent CLI registers this module after ``runner_commands`` so ``export`` and
``import`` live under the existing ``maya runner`` command group. This module
never edits ``cli/main.py`` and never calls storage directly.

Signing and verification keys are resolved *from named environment variables
only*. The CLI never accepts a literal key argument and never echoes a key
value; the only key material it reports is the public ``key_id`` recorded by the
existing attestation helpers.
"""

from __future__ import annotations

import argparse
import os
import tarfile
import tempfile
from pathlib import Path

DEFAULT_SIGNING_KEY_ENV = "MAYA_SIGNING_KEY"


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def _runner_subparsers(sub: argparse._SubParsersAction):  # type: ignore[type-arg]
    """Reuse the ``runner`` parser if the parent already registered it."""
    runner = sub.choices.get("runner") if hasattr(sub, "choices") else None
    if runner is None:
        runner = sub.add_parser(
            "runner",
            help="Inspect execution capabilities and exchange portable run bundles",
        )
        return runner.add_subparsers(dest="runner_command", metavar="<action>")
    for action in runner._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return runner.add_subparsers(dest="runner_command", metavar="<action>")


def register_runner_bundle_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    runner_sub = _runner_subparsers(sub)

    export = runner_sub.add_parser(
        "export", help="Export a run or comparison as a portable runner bundle"
    )
    group = export.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", dest="run_id", help="xrun_... run id")
    group.add_argument("--comparison", dest="comparison_id", help="xcmp_... comparison id")
    export.add_argument(
        "--out", required=True, help="Destination .tar.gz path (must not already exist)"
    )
    export.add_argument(
        "--sign",
        action="store_true",
        help="Sign the exported bundle with a key read from an environment variable",
    )
    export.add_argument(
        "--signing-key-env",
        default=DEFAULT_SIGNING_KEY_ENV,
        metavar="NAME",
        help=f"Environment variable holding the signing key (default: {DEFAULT_SIGNING_KEY_ENV})",
    )
    export.set_defaults(func=cmd_runner_export)

    imp = runner_sub.add_parser(
        "import", help="Verify and import a portable runner bundle"
    )
    imp.add_argument("bundle", help="Path to the .tar.gz bundle")
    imp.add_argument(
        "--verify-with",
        nargs="?",
        const=DEFAULT_SIGNING_KEY_ENV,
        default=None,
        metavar="NAME",
        help=(
            "Verify the attestation with the key in environment variable NAME "
            f"(bare flag uses {DEFAULT_SIGNING_KEY_ENV}); no literal key is accepted"
        ),
    )
    imp.set_defaults(func=cmd_runner_import)


def _key_from_env(name: str, *, purpose: str) -> str:
    """Resolve a key VALUE only from ``name``; never from an CLI argument."""
    from mayaworkbench.errors import UsageError

    value = os.environ.get(name)
    if not value:
        raise UsageError(
            f"No key found in environment variable {name} for {purpose}",
            hint=f"Set {name} in your environment; the key is never passed as an argument.",
        )
    return value


def _attestation_key_id(archive: Path) -> str | None:
    """Return the key_id from the bundle's envelope via the attestation helper.

    Only ``manifest.json`` and ``attestation.json`` are staged; the key itself is
    never read from the environment here, so the helper reports the claimed id
    structurally without pretending to verify a signature.
    """
    from mayaworkbench.verification.attestation import verify_attestation

    try:
        with tempfile.TemporaryDirectory(prefix="maya-bundle-attestation-") as staging_name:
            staging = Path(staging_name)
            with tarfile.open(archive, "r:gz") as bundle:
                for name in ("manifest.json", "attestation.json"):
                    handle = bundle.extractfile(bundle.getmember(name))
                    if handle is None:
                        return None
                    (staging / name).write_bytes(handle.read())
            checked = verify_attestation(staging)
        key_id = checked.get("key_id")
        return str(key_id) if key_id is not None else None
    except (OSError, tarfile.TarError, ValueError, KeyError):
        return None


def cmd_runner_export(args: argparse.Namespace) -> int:
    console = args.console
    identifier = args.run_id or args.comparison_id
    sign = bool(getattr(args, "sign", False))
    signing_key: str | None = None
    if sign:
        signing_key = _key_from_env(
            getattr(args, "signing_key_env", DEFAULT_SIGNING_KEY_ENV),
            purpose="signing this bundle",
        )
    result = _services(args).experiment_service.export_run_bundle(
        identifier, args.out, sign=sign, signing_key=signing_key
    )
    payload = {
        "path": str(result.path),
        "archive_sha256": result.archive_sha256,
        "schema_version": result.manifest.get("schema_version"),
        "files": len(result.manifest.get("files", [])),
        "signed": sign,
        "note": (
            "Imported results are unattested unless a trusted-runner signature "
            "verifies; no remote dispatch is performed."
        ),
    }
    if sign:
        payload["key_id"] = _attestation_key_id(result.path)
    if console.json_mode:
        console.json(payload)
    else:
        console.heading("Portable runner bundle exported")
        rows = [
            ("path", payload["path"]),
            ("sha256", payload["archive_sha256"]),
            ("files", payload["files"]),
        ]
        if sign:
            rows.append(("key_id", payload["key_id"]))
        console.kv(rows)
        console.warning(payload["note"])
    return 0


def cmd_runner_import(args: argparse.Namespace) -> int:
    console = args.console
    verify_with = getattr(args, "verify_with", None)
    key: str | None = None
    if verify_with is not None:
        key = _key_from_env(verify_with, purpose="verifying this bundle")
    result = _services(args).experiment_service.import_run_bundle(args.bundle, key=key)
    payload = {
        "path": str(result.path),
        "archive_sha256": result.archive_sha256,
        "attestation": result.attestation,
        "attestation_level": result.attestation_level,
        "attestation_verified": result.attestation_verified,
        "key_id": result.attestation_key_id,
        "imported_runs": len(result.created_run_ids),
        "observations": result.observation_count,
        "limitation": result.limitation,
        "notes": result.notes,
    }
    if console.json_mode:
        console.json(payload)
    else:
        console.heading("Portable runner bundle imported")
        console.kv(
            [
                ("path", payload["path"]),
                ("sha256", payload["archive_sha256"]),
                ("attestation", payload["attestation"]),
                ("imported runs", payload["imported_runs"]),
                ("observations", payload["observations"]),
            ]
        )
        console.warning(payload["limitation"])
        for note in result.notes:
            console.note(f"- {note}")
    return 0


__all__ = ["register_runner_bundle_commands"]
