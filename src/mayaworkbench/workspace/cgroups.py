"""Optional cgroup v2 process-tree limiter.

``pids.max`` is the primitive a task needs for a real per-task process budget.
This module never claims enforcement from an unverified path: a controller name
in ``cgroup.controllers`` is only *presence* evidence, the limit must be written
and read back exactly, and attachment must be verified in ``cgroup.procs``.

A finite limit can never be confirmed by a ``max`` readback — that is the
kernel's "no limit" value. Cleanup on a real cgroup filesystem uses ``rmdir``
after draining processes, never ``shutil.rmtree`` (control files are kernel
virtual files that cannot be unlinked). Failure at any step is raised so the
caller reports ``process_count: unsupported`` instead of a false green.
"""

from __future__ import annotations

import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CONTROL_FILES = frozenset(
    {
        "cgroup.procs",
        "cgroup.controllers",
        "cgroup.events",
        "cgroup.freeze",
        "cgroup.subtree_control",
        "pids.max",
        "pids.current",
        "pids.events",
        "memory.max",
        "memory.current",
        "cpu.max",
        "notify_on_release",
        "tasks",
    }
)


@dataclass(frozen=True)
class CgroupCapability:
    available: bool
    controllers: tuple[str, ...]
    reason: str = ""

    @property
    def supports_pids(self) -> bool:
        return "pids" in self.controllers


def _mounted_cgroup2_root() -> Path | None:
    """Return the path of a cgroup v2 mount, or ``None`` (presence != mount)."""
    try:
        lines = Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - non-Linux / restricted hosts
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "cgroup2":
            return Path(parts[1])
    return None


def discover_cgroup_root() -> Path | None:
    """Discover *this process's writable* cgroup v2 delegation subtree.

    Kernel controller names alone do not prove that Maya can create a child
    scope: the cgroup must be v2, mounted, the process's own cgroup path must
    exist, and the directory must be writable. Returns ``None`` when any of
    those checks fail, which keeps the caller truthful instead of optimistic.
    """
    mount = _mounted_cgroup2_root()
    if mount is None:
        return None
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - non-Linux
        return None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[0] != "0":  # "0:" is the unified v2 hierarchy
            continue
        own = parts[2].strip("/")
        root = mount if not own else mount / own
        if root.is_dir() and os.access(root, os.W_OK):
            return root
    return None


def probe_cgroup_enforcement(root: Path | None = None) -> dict[str, object]:
    """Evidence for the capability report: distinguish presence from enforcement.

    A controller name is ``present``. Writability of a child ``pids.max`` makes
    it ``configurable``. Only a readback of an exact finite value makes it
    ``applied``; ``read_back`` and ``verified_by_test`` require an actual
    process to be attached and counted. This function never reports
    ``enforced`` on controller presence alone.
    """
    chosen = root if root is not None else discover_cgroup_root()
    result: dict[str, object] = {
        "available": False,
        "mounted": _mounted_cgroup2_root() is not None,
        "delegated_root": str(chosen) if chosen is not None else None,
        "controllers": (),
        "process_count": {
            "status": "unknown",
            "mechanism": None,
            "detail": "no writable cgroup v2 delegation for this process",
        },
    }
    if chosen is None:
        return result
    capability = probe_cgroup_v2(chosen)
    result["controllers"] = capability.controllers
    if not capability.available:
        result["process_count"] = {
            "status": "present" if capability.supports_pids else "unknown",
            "mechanism": None,
            "detail": capability.reason,
        }
        return result

    scope = CgroupV2Scope(chosen, name=f"maya-probe-{os.getpid()}")
    try:
        scope.create(2)
        applied = (scope.path / "pids.max").read_text(encoding="ascii").strip() == "2"
        result["process_count"] = {
            "status": "applied" if applied else "configurable",
            "mechanism": "cgroup-v2:pids.max" if applied else None,
            "detail": (
                "finite pids.max read back from a delegated child scope"
                if applied
                else "pids.max write could not be verified by readback"
            ),
        }
        result["available"] = applied
    except Exception as exc:  # noqa: BLE001 - a probe must fail closed
        result["process_count"] = {
            "status": "present",
            "mechanism": None,
            "detail": f"delegated child scope could not be verified: {exc}",
        }
    finally:
        try:
            scope.cleanup()
        except Exception:  # noqa: BLE001 - never mask the probe result
            pass
    return result


def probe_cgroup_v2(root: Path = DEFAULT_CGROUP_ROOT) -> CgroupCapability:
    """Read controller *presence* from a cgroup root (not an enforcement proof)."""
    path = Path(root) / "cgroup.controllers"
    try:
        controllers = tuple(path.read_text(encoding="ascii").split())
    except OSError as exc:
        return CgroupCapability(False, (), f"cgroup v2 is unavailable: {exc}")
    if "pids" not in controllers:
        return CgroupCapability(
            False, controllers, "the pids controller is not delegated on this host"
        )
    return CgroupCapability(True, controllers)


class CgroupV2Scope:
    """A per-run cgroup v2 scope with a verified finite pids.max."""

    def __init__(self, root: Path = DEFAULT_CGROUP_ROOT, *, name: str = "maya-run"):
        self.root = Path(root)
        self.name = name
        self.path = self.root / name
        self._created = False

    @property
    def created(self) -> bool:
        return self._created

    def _write_and_read_pids_max(self, limit: int) -> str:
        """Write the limit, then read it back. Overridable in tests to model a
        kernel that silently ignores the write (and reports ``max``)."""
        (self.path / "pids.max").write_text(str(limit), encoding="ascii")
        return (self.path / "pids.max").read_text(encoding="ascii").strip()

    def create(self, pids_limit: int) -> None:
        if pids_limit < 1:
            raise ValueError("pids limit must be positive")
        if self._created:
            return
        capability = probe_cgroup_v2(self.root)
        if not capability.available:
            raise RuntimeError(capability.reason)
        self.path.mkdir(parents=True, exist_ok=False)
        self._created = True
        try:
            observed = self._write_and_read_pids_max(pids_limit)
            # "max" is the kernel's unlimited value. Accepting it for a finite
            # request is a silent false-green: the budget would not be enforced.
            if observed != str(pids_limit):
                raise RuntimeError(
                    f"cgroup pids.max readback mismatch: expected {pids_limit}, got {observed!r}"
                )
        except Exception:
            self.cleanup()
            raise

    def attach(self, pid: int) -> None:
        if not self._created:
            raise RuntimeError("scope was not created")
        (self.path / "cgroup.procs").write_text(str(pid), encoding="ascii")
        present = (self.path / "cgroup.procs").read_text(encoding="ascii").split()
        if str(pid) not in present:
            raise RuntimeError(f"pid {pid} was not attached to the cgroup")

    def pids_current(self) -> int:
        try:
            return int((self.path / "pids.current").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return 0

    def _live_pids(self) -> list[str]:
        procs = self.path / "cgroup.procs"
        try:
            pids = procs.read_text(encoding="ascii").split()
        except OSError:
            return []
        live: list[str] = []
        for pid in pids:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue  # already reaped; a stale cgroup.procs entry is harmless
            except (PermissionError, ValueError):
                live.append(pid)
            else:
                live.append(pid)
        return live

    def cleanup(self) -> None:
        """Drain the scope and remove the directory with the correct primitive.

        On a real cgroup filesystem control files cannot be unlinked, so
        ``rmdir`` is the only valid removal and a failure must be raised.
        ``shutil.rmtree`` is used only for stand-in filesystem trees (test
        fixtures), which are ordinary directories and are never mistaken for
        a real enforcement boundary.
        """
        if not self._created:
            return
        procs = self.path / "cgroup.procs"
        parent_procs = self.root / "cgroup.procs"
        for pid in self._live_pids():
            moved = False
            if parent_procs.exists():
                try:
                    parent_procs.write_text(pid, encoding="ascii")
                    moved = pid in parent_procs.read_text(encoding="ascii").split()
                except OSError:
                    moved = False
            if not moved:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, ValueError):
                    pass
        survivors = self._live_pids()
        if survivors:
            raise RuntimeError(
                f"cgroup scope still has live processes; refusing removal: {survivors}"
            )
        if not self.path.exists():
            self._created = False
            return
        if _mounted_cgroup2_root() is not None and _is_under_cgroup_mount(self.path):
            try:
                self.path.rmdir()  # cgroupfs: the only correct removal primitive
            except OSError as exc:
                raise RuntimeError(f"cgroup scope cleanup failed: {exc}") from exc
        else:
            # Stand-in tree (tests / non-cgroupfs). Never used for a real scope.
            shutil.rmtree(self.path)
        self._created = False


def _is_under_cgroup_mount(path: Path) -> bool:
    mount = _mounted_cgroup2_root()
    if mount is None:
        return False
    try:
        path.resolve().relative_to(mount.resolve())
    except ValueError:
        return False
    return True


__all__ = [
    "CgroupCapability",
    "CgroupV2Scope",
    "probe_cgroup_v2",
    "probe_cgroup_enforcement",
    "discover_cgroup_root",
    "DEFAULT_CGROUP_ROOT",
]
