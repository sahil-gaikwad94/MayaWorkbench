"""Content-addressed artifact storage.

Large or evidence-bearing content (file snapshots, patches, context packs,
answers) is written under ``$MAYA_HOME/artifacts`` and referenced from the
database by an ``Artifact`` record. Identity is the SHA-256 of the bytes, so
deduplication is automatic and tampering is detectable on read.

This is a local filesystem store by design (§16.1). It is not a distributed
object store; the interface is narrow enough to add one later.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from mayaworkbench.clock import utcnow
from mayaworkbench.domain.enums import ArtifactKind, RetentionClass
from mayaworkbench.domain.models import Artifact
from mayaworkbench.errors import ConflictError, DatabaseError, NotFoundError
from mayaworkbench.ids import short_hash
from mayaworkbench.storage.db import Database


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = Path(root)

    # ------------------------------------------------------------------ write
    def put_bytes(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        media_type: str,
        created_by: str = "system",
        retention: RetentionClass = RetentionClass.PROJECT,
    ) -> Artifact:
        digest = _sha256(data)
        existing = self.find_by_sha(digest, kind)
        if existing is not None:
            self._ensure_blob(existing, data)
            return existing

        rel_path = f"{kind.value.lower()}/{digest[:2]}/{digest}"
        target = self.root / rel_path
        self._write_atomic(target, data)
        artifact = Artifact(
            id="art_" + short_hash(f"{digest}:{kind.value}", 32),
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
            kind=kind,
            rel_path=rel_path,
            retention=retention,
            created_by=created_by,
            created_at=utcnow(),
        )
        self.db.execute(
            """
            INSERT INTO artifacts (id, sha256, size_bytes, media_type, kind, rel_path,
                                   retention, created_by, created_at)
            VALUES (:id, :sha256, :size_bytes, :media_type, :kind, :rel_path,
                    :retention, :created_by, :created_at)
            """,
            {
                **artifact.model_dump(mode="json"),
                "retention": artifact.retention.value,
            },
        )
        return artifact

    def put_text(
        self,
        text: str,
        *,
        kind: ArtifactKind,
        media_type: str = "text/plain; charset=utf-8",
        created_by: str = "system",
        retention: RetentionClass = RetentionClass.PROJECT,
    ) -> Artifact:
        return self.put_bytes(
            text.encode("utf-8"),
            kind=kind,
            media_type=media_type,
            created_by=created_by,
            retention=retention,
        )

    def _write_atomic(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except OSError as exc:
            Path(tmp_name).unlink(missing_ok=True)
            raise DatabaseError(f"Cannot write artifact {target}: {exc}") from exc
        if os.name == "posix":
            try:
                target.chmod(0o600)
            except OSError:  # pragma: no cover
                pass

    def _ensure_blob(self, artifact: Artifact, data: bytes) -> None:
        """Repair a missing blob for an existing artifact row."""
        target = self.root / artifact.rel_path
        if not target.exists():
            self._write_atomic(target, data)

    # ------------------------------------------------------------------- read
    def get(self, artifact_id: str) -> Artifact:
        row = self.db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if row is None:
            raise NotFoundError(f"Artifact {artifact_id} does not exist")
        return self._row_to_model(row)

    def find_by_sha(self, sha256: str, kind: ArtifactKind) -> Artifact | None:
        row = self.db.fetchone(
            "SELECT * FROM artifacts WHERE sha256 = ? AND kind = ?", (sha256, kind.value)
        )
        return self._row_to_model(row) if row else None

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        target = self.root / artifact.rel_path
        try:
            data = target.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(
                f"Artifact {artifact_id} metadata exists but its content is missing",
                hint="The artifact store was modified outside MayaWorkbench.",
            ) from exc
        except OSError as exc:
            raise DatabaseError(f"Cannot read artifact {artifact_id}: {exc}") from exc
        digest = _sha256(data)
        if digest != artifact.sha256:
            raise ConflictError(
                f"Artifact {artifact_id} failed integrity check "
                f"(expected {artifact.sha256[:12]}, found {digest[:12]})",
                hint="Treat this evidence as corrupted and re-create the snapshot/index.",
            )
        return data

    def read_text(self, artifact_id: str) -> str:
        try:
            return self.read_bytes(artifact_id).decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - text artifacts are UTF-8
            raise DatabaseError(f"Artifact {artifact_id} is not valid UTF-8: {exc}") from exc

    def list(self, kind: ArtifactKind | None = None, limit: int = 100) -> list[Artifact]:
        if kind is None:
            rows = self.db.fetchall(
                "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM artifacts WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                (kind.value, limit),
            )
        return [self._row_to_model(row) for row in rows]

    def resolve_path(self, artifact_id: str) -> Path:
        artifact = self.get(artifact_id)
        return self.root / artifact.rel_path

    # ------------------------------------------------------------------ mapper
    @staticmethod
    def _row_to_model(row) -> Artifact:  # type: ignore[no-untyped-def]
        return Artifact.model_validate(
            {
                "id": row["id"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "media_type": row["media_type"],
                "kind": row["kind"],
                "rel_path": row["rel_path"],
                "retention": row["retention"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
        )


__all__ = ["ArtifactStore"]
