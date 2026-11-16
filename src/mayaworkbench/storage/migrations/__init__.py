"""SQL migration loading.

Migrations are plain ``.sql`` files named ``NNNN_description.sql``. They are
checksummed: if an already-applied migration's bytes change, the database
refuses to open rather than running with a schema that does not match its
history.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

_NAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations() -> list[Migration]:
    package = resources.files(__name__)
    discovered: list[Migration] = []
    for entry in package.iterdir():
        filename = entry.name
        match = _NAME_RE.match(filename)
        if not match:
            continue
        sql = entry.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        discovered.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=sql,
                checksum=checksum,
            )
        )
    discovered.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in discovered]
    if len(set(versions)) != len(versions):
        raise RuntimeError("duplicate migration version numbers")
    return discovered


__all__ = ["Migration", "load_migrations"]
