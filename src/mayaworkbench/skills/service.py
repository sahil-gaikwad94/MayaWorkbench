"""Skills service: versioned, user-governed reusable procedures.

All SQL lives here (skills are their own bounded aggregate). A skill never grants
authority: ``execute`` only renders a procedure or runs deterministic static checks,
unless a future executor is explicitly wired by policy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mayaworkbench.clock import utcnow
from mayaworkbench.errors import ConflictError, NotFoundError, UsageError
from mayaworkbench.ids import new_ulid
from mayaworkbench.skills.models import Skill

_JSON = {"details": ("details_json", {})}


class SkillsService:
    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------- writes
    def upsert(
        self,
        project_id: str,
        key: str,
        name: str,
        *,
        category: str = "procedure",
        title: str = "",
        description: str = "",
        body: str = "",
        scope: str = "project",
        enabled: bool = True,
        details: dict[str, Any] | None = None,
    ) -> Skill:
        key = key.strip()
        if not key or not name.strip():
            raise UsageError("A skill needs a key and a name")
        if scope not in {"project", "user"}:
            raise UsageError(f"Unknown skill scope {scope!r}")
        row = self.db.fetchone(
            "SELECT MAX(version) AS v FROM skills WHERE project_id = ? AND key = ?",
            (project_id, key),
        )
        version = int(row["v"] or 0) + 1
        now = utcnow().isoformat()
        skill = Skill(
            id="skill_" + new_ulid(),
            project_id=project_id,
            key=key,
            version=version,
            name=name.strip(),
            category=category,
            title=title.strip(),
            description=description.strip(),
            body=body,
            scope=scope,
            enabled=enabled,
            details=details or {},
            created_at=now,
            updated_at=now,
        )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO skills (id, project_id, key, version, name, category, title, "
                "description, body, scope, enabled, details_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    skill.id, skill.project_id, skill.key, skill.version, skill.name,
                    skill.category, skill.title, skill.description, skill.body,
                    skill.scope, int(skill.enabled),
                    json.dumps(skill.details, sort_keys=True),
                    skill.created_at, skill.updated_at,
                ),
            )
        return skill

    def set_enabled(self, skill_id: str, enabled: bool) -> Skill:
        skill = self.get(skill_id)
        with self.db.transaction():
            self.db.execute(
                "UPDATE skills SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), utcnow().isoformat(), skill.id),
            )
        return self.get(skill.id)

    # -------------------------------------------------------------- reads
    def get(self, skill_id: str) -> Skill:
        row = self.db.fetchone("SELECT * FROM skills WHERE id = ?", (skill_id,))
        if row is None:
            raise NotFoundError(f"Skill not found: {skill_id}")
        return self._row(row)

    def list(
        self,
        project_id: str,
        *,
        enabled: bool | None = None,
        category: str | None = None,
        limit: int = 200,
    ) -> list[Skill]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        if category:
            clauses.append("category = ?")
            params.append(category)
        params.append(max(1, min(int(limit), 1000)))
        rows = self.db.fetchall(
            "SELECT * FROM skills WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC, id LIMIT ?",
            tuple(params),
        )
        return [self._row(row) for row in rows]

    def effective(self, project_id: str, key: str) -> Skill | None:
        rows = self.db.fetchall(
            "SELECT * FROM skills WHERE project_id = ? AND key = ? AND enabled = 1 "
            "ORDER BY version DESC LIMIT 1",
            (project_id, key),
        )
        return self._row(rows[0]) if rows else None

    def promote(self, project_id: str, key: str) -> Skill:
        skill = self.effective(project_id, key)
        if skill is None:
            raise NotFoundError(f"No enabled skill for key {key!r}")
        return skill

    # ---------------------------------------------------------- bundle IO
    def export_bundle(self, project_id: str, *, key: str | None = None) -> list[dict[str, Any]]:
        skills = self.list(project_id, limit=1000)
        if key:
            skills = [skill for skill in skills if skill.key == key]
        return [skill.model_dump(mode="json") for skill in skills]

    def import_bundle(
        self, project_id: str, bundle: list[dict[str, Any]], *, allow_conflicts: bool = False
    ) -> dict[str, Any]:
        created: list[str] = []
        skipped: list[str] = []
        conflicts: list[str] = []
        for item in bundle:
            try:
                raw = Skill.model_validate({**item, "project_id": project_id, "id": "skill_" + new_ulid()})
            except Exception as exc:  # noqa: BLE001
                conflicts.append(f"invalid skill record: {exc}")
                continue
            if not allow_conflicts:
                newer = self.db.fetchone(
                    "SELECT id FROM skills WHERE project_id = ? AND key = ? AND version > ?",
                    (project_id, raw.key, raw.version),
                )
                if newer is not None:
                    conflicts.append(raw.effective_key)
                    continue
            existing = self.db.fetchone(
                "SELECT id FROM skills WHERE project_id = ? AND key = ? AND version = ?",
                (project_id, raw.key, raw.version),
            )
            if existing is not None:
                skipped.append(raw.effective_key)
                continue
            self.upsert(
                project_id, raw.key, raw.name, category=raw.category, title=raw.title,
                description=raw.description, body=raw.body, scope=raw.scope,
                enabled=raw.enabled, details=raw.details,
            )
            created.append(raw.effective_key)
        return {"created": created, "skipped": skipped, "conflicts": conflicts}

    def seed_defaults(self, project_id: str) -> list[Skill]:
        from mayaworkbench.skills.seeds import DEFAULT_SKILLS

        created = []
        for seed in DEFAULT_SKILLS:
            if self.effective(project_id, seed["key"]) is not None:
                continue
            created.append(self.upsert(project_id, **seed))
        return created

    @staticmethod
    def _row(row: Any) -> Skill:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["details"] = json.loads(data.pop("details_json") or "{}")
        return Skill.model_validate(data)


__all__ = ["SkillsService"]
