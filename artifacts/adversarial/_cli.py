"""Resolve the MayaWorkbench CLI robustly for adversarial harnesses.

The harnesses must work from a source checkout without a prebuilt `.venv`.
Preference order:

1. an installed ``maya`` console script on PATH;
2. ``<current interpreter> -m mayaworkbench.cli.main`` (works with PYTHONPATH=src
   or an editable install);
3. a repository-local ``.venv/bin/maya`` if one happens to exist.

Never hardcode a single installation layout.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]


def maya_argv(*args: str) -> list[str]:
    """Return argv that invokes the CLI using whatever is available."""
    installed = shutil.which("maya")
    if installed:
        return [installed, *args]
    venv_cli = ROOT / ".venv" / "bin" / "maya"
    if venv_cli.is_file():
        return [str(venv_cli), *args]
    env = dict(os.environ)
    src = str(ROOT / "src")
    pythonpath = env.get("PYTHONPATH", "")
    if src not in pythonpath.split(os.pathsep):
        env["PYTHONPATH"] = f"{src}{os.pathsep}{pythonpath}" if pythonpath else src
    os.environ.update({"PYTHONPATH": env["PYTHONPATH"]})
    return [sys.executable, "-m", "mayaworkbench.cli.main", *args]


def maya_shell_command(args: Sequence[str]) -> str:
    """Return a shell-quoted command for probes that pipe CLI output."""
    import shlex

    return " ".join(shlex.quote(part) for part in maya_argv(*args))


__all__ = ["maya_argv", "maya_shell_command", "ROOT"]
