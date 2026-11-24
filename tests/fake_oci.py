"""A fake OCI client used by backend and container-session tests.

It is a *stand-in for a kernel*, not a container runtime. It parses the flags the
backend actually passes, records every invocation in ``oci-calls.jsonl`` under
the working directory, and answers like a container would. What the tests then
assert is the backend's own comparison logic (``observed == requested``) and the
exact argv it produced — never that a real container ran.

Modes and the marker file that switches them:

* ``run`` (default): prints ``container-ok``.
* ``run ... MAYA_PROBE ...``: prints one ``MAYA_PROBE {json}`` line.
* ``run --detach``: records ``session-containers.txt`` and prints the name.
  ``session-fail`` in this directory makes it exit 1 instead.
* ``exec``: prints ``session-ok`` and echoes stdin as ``echo:<line>``.
  An argument ending in ``.kill`` prints ``killed`` and exits 0.
* ``inspect``: prints ``true`` (``false`` when ``session-down`` exists).
* ``stop`` / ``rm``: print a confirmation.

Marker files live next to the fake because the command runner's child
environment is an allowlist, so markers cannot be passed through env vars.
"""

from __future__ import annotations

from pathlib import Path

FAKE_OCI_SCRIPT = '''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
log = os.path.join(here, "oci-calls.jsonl")
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")


def flag(name):
    if name in args:
        return args[args.index(name) + 1]
    return None


def marker(name):
    return os.path.exists(os.path.join(here, name))


mode = args[0] if args else ""

if mode == "rm":
    print("removed")
    sys.exit(0)

if mode == "stop":
    print(args[-1] if args else "stopped")
    sys.exit(0)

if mode == "inspect":
    print("false" if marker("session-down") else "true")
    sys.exit(0)

if mode == "exec":
    if any(item.endswith(".kill") for item in args):
        print("killed")
        sys.exit(0)
    sys.stdout.write("session-ok\\n")
    sys.stdout.flush()
    for line in sys.stdin:
        sys.stdout.write("echo:" + line)
        sys.stdout.flush()
    sys.exit(0)

if mode == "run" and "--detach" in args:
    if marker("session-fail"):
        sys.stderr.write("Error: the session container could not be created\\n")
        sys.exit(125)
    name = flag("--name") or "session"
    with open(os.path.join(os.getcwd(), "session-containers.txt"), "a", encoding="utf-8") as handle:
        handle.write(name + "\\n")
    print(name)
    sys.exit(0)

if "MAYA_PROBE" in " ".join(args):
    ulimits = {}
    for index, item in enumerate(args):
        if item == "--ulimit" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            ulimits[key] = int(value.split(":")[0])
    pids = int(flag("--pids-limit") or 8)
    memory_mb = int(str(flag("--memory") or "512m").rstrip("m"))
    cpus = float(flag("--cpus") or 1)
    memory_bytes = memory_mb * 1024 * 1024
    # A marker next to this fake makes it report a different effective value,
    # modelling a runtime that silently ignored the flag.
    if marker("lie-memory"):
        memory_bytes *= 2
    payload = {
        "pids_max": str(pids),
        "memory_max": str(memory_bytes),
        "cpu_max": "%d 100000" % int(round(cpus * 100000)),
        "pids_children": max(1, pids - 1),
        "cpu_time_child": "signal:24",
        "rlimit_cpu": [ulimits.get("cpu", 0), ulimits.get("cpu", 0)],
        "rlimit_fsize": [ulimits.get("fsize", 0), ulimits.get("fsize", 0)],
        "rlimit_nofile": [ulimits.get("nofile", 0), ulimits.get("nofile", 0)],
        "network_egress": "denied",
        "work_secret_read": "absent",
        "source_secret_read": "absent",
        "host_canary_read": "absent",
        "root_write": "denied:OSError",
        "uid": 1000,
    }
    print("MAYA_PROBE " + json.dumps(payload))
else:
    print("container-ok")
'''


def install_fake_oci(directory: Path) -> Path:
    """Write an executable ``podman`` fake into ``directory`` and return it."""

    script = directory / "podman"
    script.write_text(FAKE_OCI_SCRIPT, encoding="utf-8")
    script.chmod(0o700)
    return script


def oci_calls(directory) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """Every client invocation recorded next to the fake, in order."""

    import json

    log = Path(directory) / "oci-calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


__all__ = ["FAKE_OCI_SCRIPT", "install_fake_oci", "oci_calls"]
