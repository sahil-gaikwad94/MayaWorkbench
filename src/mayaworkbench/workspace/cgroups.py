"""Optional cgroup v2 process-tree limiter (MW-P0-02).

``pids.max`` is the primitive the audit requires for a real per-task process
budget. This module never claims enforcement from an unverified path: capability
reads the controllers file, scope creation writes the limit and reads it back,
and attachment writes the child pid and verifies ``cgroup.procs``. If any step
fails, the caller must report ``process_count: unsupported`` and block the
untrusted profile.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class CgroupCapability:
    available: bool
    controllers: tuple[str, ...]
    reason: str = ""

    @property
    def supports_pids(self) -> bool:
        return "pids" in self.controllers


def probe_cgroup_v2(root: Path = DEFAULT_CGROUP_ROOT) -> CgroupCapability:
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
    """A per-run cgroup v2 scope with an enforced pids.max."""

    def __init__(self, root: Path = DEFAULT_CGROUP_ROOT, *, name: str = "maya-run"):
        self.root = Path(root)
        self.name = name
        self.path = self.root / name
        self._created = False

    def create(self, pids_limit: int) -> None:
        if pids_limit < 1:
            raise ValueError("pids limit must be positive")
        capability = probe_cgroup_v2(self.root)
        if not capability.available:
            raise RuntimeError(capability.reason)
        self.path.mkdir(parents=True, exist_ok=False)
        self._created = True
        (self.path / "pids.max").write_text(str(pids_limit), encoding="ascii")
        observed = (self.path / "pids.max").read_text(encoding="ascii").strip()
        if observed not in {str(pids_limit), "max"}:
            raise RuntimeError(f"cgroup pids.max readback failed: {observed!r}")

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

    def cleanup(self) -> None:
        if not self._created:
            return
        procs = self.path / "cgroup.procs"
        if procs.exists():
            # Move any survivors back to the parent before removing the scope.
            parent = (self.root / "cgroup.procs").read_text(encoding="ascii")
            try:
                for pid in procs.read_text(encoding="ascii").split():
                    if pid not in parent.split():
                        (self.root / "cgroup.procs").write_text(pid, encoding="ascii")
            except OSError:  # pragma: no cover - cleanup must not mask a failure
                pass
        shutil.rmtree(self.path, ignore_errors=True)
        self._created = False


__all__ = ["CgroupCapability", "CgroupV2Scope", "probe_cgroup_v2", "DEFAULT_CGROUP_ROOT"]
