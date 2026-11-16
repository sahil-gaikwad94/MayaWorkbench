"""Structured logging.

Two sinks:

* a JSON-lines rotating file under ``$MAYA_HOME/logs/maya.log`` for durable
  diagnostics and later export;
* stderr only for warnings and errors, so normal CLI output stays clean.

Log records never include file contents, prompts, or credentials. They carry
identifiers (project/snapshot/run) so an operator can correlate a log line with
the append-only event log in the database.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

_CONFIGURED = False
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class StderrFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        prefix = record.levelname.lower()
        return f"maya: {prefix}: {record.getMessage()}"


def configure_logging(
    log_dir: Path | None,
    *,
    verbose: bool = False,
    quiet: bool = False,
    force: bool = False,
) -> None:
    """Configure the ``mayaworkbench`` logger tree exactly once per process."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger("mayaworkbench")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(StderrFormatter())
    stderr_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(stderr_handler)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "maya.log",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(JsonFormatter())
            file_handler.setLevel(logging.DEBUG)
            root.addHandler(file_handler)
            if os.name == "posix":
                try:
                    (log_dir / "maya.log").touch(exist_ok=True)
                    (log_dir / "maya.log").chmod(0o600)
                except OSError:  # pragma: no cover
                    pass
        except OSError:
            # Logging must never break the command being run.
            root.debug("file logging unavailable", extra={"log_dir": str(log_dir)})

    if quiet:
        stderr_handler.setLevel(logging.CRITICAL)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mayaworkbench.{name}")


__all__ = ["configure_logging", "get_logger", "JsonFormatter", "StderrFormatter"]
