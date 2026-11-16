"""Reference resolution.

Static resolution in a dynamic language is inherently partial. This resolver
prefers precision over recall and labels every edge with a confidence:

* ``HIGH`` — exact unique match through a local definition or an explicit import;
* ``MEDIUM`` — unique match via a module-suffix or a unique global name;
* ``LOW`` — unresolved, or ambiguous (the target is recorded textually).

A name match is never presented as proof of semantic correspondence (§9.4); it
is a navigational aid whose confidence is visible in every consumer.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from mayaworkbench.domain.enums import EdgeConfidence
from mayaworkbench.domain.models import IndexedFile
from mayaworkbench.indexing.languages import module_name_for_path

_CALL_CHARS = re.compile(r"[\s()]")
_SELF_PREFIXES = ("self.", "cls.")


def clean_target_name(name: str) -> str:
    name = name.strip()
    name = name.split("(", 1)[0]
    name = name.split(" ", 1)[0]
    return name.strip()


def absolute_module(importer_path: str, level: int, module: str) -> str:
    """Resolve a relative import to a dotted module path."""
    importer = module_name_for_path(importer_path)
    base = importer.split(".") if importer else []
    if not importer_path.endswith("__init__.py"):
        base = base[:-1]
    if level > 1:
        base = base[: max(0, len(base) - (level - 1))]
    prefix = ".".join(part for part in base if part)
    if prefix and module:
        return f"{prefix}.{module}"
    return prefix or module


def module_keys(path: str) -> list[str]:
    dotted = module_name_for_path(path)
    if not dotted:
        return []
    parts = dotted.split(".")
    keys = [dotted]
    for index in range(1, len(parts)):
        suffix = ".".join(parts[index:])
        if suffix and suffix not in keys:
            keys.append(suffix)
    return keys


class ModuleIndex:
    def __init__(self, files: list[IndexedFile]):
        self.exact: dict[str, list[IndexedFile]] = defaultdict(list)
        self.suffix: dict[str, list[IndexedFile]] = defaultdict(list)
        for file in files:
            if file.language != "python":
                continue
            keys = module_keys(file.path)
            if not keys:
                continue
            self.exact[keys[0]].append(file)
            for key in keys[1:]:
                self.suffix[key].append(file)

    def lookup(self, module: str) -> tuple[list[IndexedFile], EdgeConfidence | None]:
        if not module:
            return [], None
        exact = self.exact.get(module)
        if exact:
            return exact, EdgeConfidence.HIGH
        suffix = self.suffix.get(module)
        if suffix:
            return suffix, EdgeConfidence.MEDIUM
        return [], None


@dataclass(frozen=True)
class ResolvedTarget:
    symbol_id: str | None
    file_id: str | None
    confidence: EdgeConfidence
    reason: str

    @property
    def resolved(self) -> bool:
        return self.symbol_id is not None or self.file_id is not None


UNRESOLVED = ResolvedTarget(None, None, EdgeConfidence.LOW, "no static match")


class EdgeResolver:
    def __init__(
        self,
        *,
        files: list[IndexedFile],
        symbols: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        aliases: list[dict[str, Any]],
    ):
        self.files = files
        self.file_by_id = {file.id: file for file in files}
        self.module_index = ModuleIndex(files)
        self.symbols_by_id = {symbol["id"]: symbol for symbol in symbols}

        self.by_file_qualified: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.by_file_name: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.module_symbol_by_file: dict[str, dict[str, Any]] = {}
        self.global_callables: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for symbol in symbols:
            self.by_file_qualified[symbol["file_id"]][symbol["qualified_name"]] = symbol
            self.by_file_name[symbol["file_id"]][symbol["name"]].append(symbol)
            if symbol["kind"] == "module":
                self.module_symbol_by_file[symbol["file_id"]] = symbol
            if symbol["kind"] in ("class", "function"):
                self.global_callables[symbol["name"]].append(symbol)

        self.aliases_by_file: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.star_import_files: set[str] = set()
        for alias in aliases:
            self.aliases_by_file[alias["file_id"]][alias["alias"]].append(alias)
            if alias.get("imported_name") == "*":
                self.star_import_files.add(alias["file_id"])

        self._alias_cache: dict[tuple[str, str], ResolvedTarget] = {}
        self.edges = edges

    # ------------------------------------------------------------------ public
    def resolve_all(self) -> tuple[list[tuple[str | None, str | None, str, str]], list[dict]]:
        """Return ``(updates, unresolved_relative_imports)``."""
        updates: list[tuple[str | None, str | None, str, str]] = []
        unresolved_imports: list[dict] = []
        for edge in self.edges:
            target_name = clean_target_name(edge["target_name"])
            if not target_name:
                continue
            if edge["edge_type"] == "IMPORTS":
                resolved = self._resolve_import(target_name, edge["source_file_id"])
                if not resolved.resolved and target_name.startswith("."):
                    unresolved_imports.append(
                        {
                            "file_id": edge["source_file_id"],
                            "path": self._path(edge["source_file_id"]),
                            "target": target_name,
                            "line": edge["line"],
                        }
                    )
            else:
                resolved = self._resolve_name(
                    target_name,
                    edge["source_file_id"],
                    source_symbol_id=edge["source_symbol_id"],
                )
            if not resolved.resolved:
                if edge["confidence"] != EdgeConfidence.LOW.value:
                    updates.append((None, None, EdgeConfidence.LOW.value, edge["id"]))
                continue
            updates.append(
                (resolved.symbol_id, resolved.file_id, resolved.confidence.value, edge["id"])
            )
        return updates, unresolved_imports

    # ----------------------------------------------------------------- imports
    def _resolve_import(self, module: str, source_file_id: str) -> ResolvedTarget:
        path = self._path(source_file_id)
        if not path:
            return UNRESOLVED
        level = len(module) - len(module.lstrip("."))
        bare = module[level:]
        absolute = absolute_module(path, level, bare) if level else bare
        candidates, confidence = self.module_index.lookup(absolute)
        if not candidates:
            # `from pkg import submodule` style: the aliases carry the submodule.
            return UNRESOLVED
        if len(candidates) == 1:
            target = candidates[0]
            module_symbol = self.module_symbol_by_file.get(target.id)
            return ResolvedTarget(
                symbol_id=module_symbol["id"] if module_symbol else None,
                file_id=target.id,
                confidence=confidence or EdgeConfidence.MEDIUM,
                reason=f"module {absolute}",
            )
        return ResolvedTarget(
            None, None, EdgeConfidence.LOW, f"ambiguous module {absolute} ({len(candidates)} matches)"
        )

    # ------------------------------------------------------------------- names
    def _resolve_name(
        self, name: str, source_file_id: str, *, source_symbol_id: str | None
    ) -> ResolvedTarget:
        parts = [part for part in name.split(".") if part]
        if not parts:
            return UNRESOLVED

        if name.startswith(_SELF_PREFIXES):
            resolved = self._resolve_self(parts, source_file_id, source_symbol_id)
            if resolved.resolved:
                return resolved

        local = self._resolve_local(parts, source_file_id, source_symbol_id)
        if local.resolved:
            return local

        aliased = self._resolve_alias(parts, source_file_id)
        if aliased.resolved:
            return aliased

        modular = self._resolve_module_prefix(parts, source_file_id)
        if modular.resolved:
            return modular

        if len(parts) == 1:
            return self._resolve_global(parts[0], source_file_id)
        return UNRESOLVED

    def _resolve_self(
        self, parts: list[str], source_file_id: str, source_symbol_id: str | None
    ) -> ResolvedTarget:
        if not source_symbol_id:
            return UNRESOLVED
        source = self.symbols_by_id.get(source_symbol_id)
        if source is None:
            return UNRESOLVED
        parent = source.get("parent_symbol_id")
        if not parent:
            return UNRESOLVED
        enclosing = self.symbols_by_id.get(parent)
        if enclosing is None or len(parts) < 2:
            return UNRESOLVED
        candidate = self.by_file_qualified[source_file_id].get(
            f"{enclosing['qualified_name']}.{parts[1]}"
        )
        if candidate is not None:
            return ResolvedTarget(
                candidate["id"],
                source_file_id,
                EdgeConfidence.HIGH,
                f"member of {enclosing['qualified_name']}",
            )
        return UNRESOLVED

    def _resolve_local(
        self, parts: list[str], source_file_id: str, source_symbol_id: str | None
    ) -> ResolvedTarget:
        """Resolve a bare name to a definition that is visible from the caller.

        A same-file definition is not automatically visible: a function nested
        inside another function, or a method on a different class, cannot be
        called by its bare name. Treating those as HIGH would be a false claim,
        so only module-level definitions and definitions in an enclosing scope
        qualify.
        """
        qualified_map = self.by_file_qualified.get(source_file_id, {})
        name_map = self.by_file_name.get(source_file_id, {})
        caller_scope = self._caller_scope(source_symbol_id)
        candidates = [
            candidate
            for candidate in name_map.get(parts[0], [])
            if self._visible_from(candidate, caller_scope)
        ]
        if len(candidates) != 1:
            return UNRESOLVED
        candidate = candidates[0]
        if len(parts) == 1:
            return ResolvedTarget(
                candidate["id"], source_file_id, EdgeConfidence.HIGH, "same-file definition"
            )
        nested = qualified_map.get(f"{candidate['qualified_name']}.{parts[1]}")
        if nested is not None:
            return ResolvedTarget(
                nested["id"],
                source_file_id,
                EdgeConfidence.HIGH,
                f"member of same-file {candidate['qualified_name']}",
            )
        return UNRESOLVED

    def _caller_scope(self, source_symbol_id: str | None) -> str:
        if not source_symbol_id:
            return ""
        symbol = self.symbols_by_id.get(source_symbol_id)
        return str(symbol["qualified_name"]) if symbol else ""

    def _visible_from(self, candidate: dict[str, Any], caller_scope: str) -> bool:
        parent_id = candidate.get("parent_symbol_id")
        if not parent_id:
            return True
        parent = self.symbols_by_id.get(str(parent_id))
        if parent is None:
            return False
        parent_name = str(parent["qualified_name"])
        return caller_scope == parent_name or caller_scope.startswith(parent_name + ".")

    def _resolve_alias(self, parts: list[str], source_file_id: str) -> ResolvedTarget:
        alias_rows = self.aliases_by_file.get(source_file_id, {}).get(parts[0])
        if not alias_rows:
            return UNRESOLVED
        for row in alias_rows:
            target = self._alias_target(row)
            if not target.resolved:
                continue
            if len(parts) == 1:
                return target
            if target.symbol_id:
                symbol = self.symbols_by_id.get(target.symbol_id)
                if symbol is not None:
                    nested = self.by_file_qualified.get(symbol["file_id"], {}).get(
                        f"{symbol['qualified_name']}.{parts[1]}"
                    )
                    if nested is not None:
                        return ResolvedTarget(
                            nested["id"],
                            symbol["file_id"],
                            EdgeConfidence.HIGH,
                            f"member of imported {symbol['qualified_name']}",
                        )
            if target.file_id:
                nested = self.by_file_name.get(target.file_id, {}).get(parts[1], [])
                if len(nested) == 1:
                    return ResolvedTarget(
                        nested[0]["id"],
                        target.file_id,
                        EdgeConfidence.MEDIUM,
                        f"{parts[1]} in imported module {row['module']}",
                    )
        return UNRESOLVED

    def _alias_target(self, row: dict[str, Any]) -> ResolvedTarget:
        cache_key = (row["file_id"], f"{row['module']}|{row['imported_name']}|{row['level']}")
        cached = self._alias_cache.get(cache_key)
        if cached is not None:
            return cached
        path = self._path(row["file_id"])
        result = UNRESOLVED
        if path:
            level = int(row["level"])
            module = row["module"]
            absolute = (
                absolute_module(path, level, module) if level and module else module
            )
            candidates: list[IndexedFile] = []
            confidence: EdgeConfidence | None = None
            for attempt in (absolute, f"{absolute}.{row['imported_name']}".strip(".")):
                if not attempt:
                    continue
                candidates, confidence = self.module_index.lookup(attempt)
                if candidates:
                    break
            if len(candidates) == 1:
                target_file = candidates[0]
                imported_name = row["imported_name"]
                if imported_name in ("*", ""):
                    symbol = self.module_symbol_by_file.get(target_file.id)
                    result = ResolvedTarget(
                        symbol["id"] if symbol else None,
                        target_file.id,
                        confidence or EdgeConfidence.MEDIUM,
                        f"star import from {absolute}",
                    )
                else:
                    matches = self.by_file_name.get(target_file.id, {}).get(imported_name, [])
                    if len(matches) == 1:
                        result = ResolvedTarget(
                            matches[0]["id"],
                            target_file.id,
                            EdgeConfidence.HIGH,
                            f"imported {imported_name} from {absolute}",
                        )
                    elif matches:
                        result = ResolvedTarget(
                            None,
                            target_file.id,
                            EdgeConfidence.LOW,
                            f"ambiguous imported name {imported_name}",
                        )
                    else:
                        module_symbol = self.module_symbol_by_file.get(target_file.id)
                        result = ResolvedTarget(
                            module_symbol["id"] if module_symbol else None,
                            target_file.id,
                            EdgeConfidence.MEDIUM,
                            f"module import {absolute}",
                        )
            elif len(candidates) > 1:
                result = ResolvedTarget(
                    None,
                    None,
                    EdgeConfidence.LOW,
                    f"ambiguous import {absolute}",
                )
        self._alias_cache[cache_key] = result
        return result

    def _resolve_module_prefix(self, parts: list[str], source_file_id: str) -> ResolvedTarget:
        for split in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:split])
            candidates, confidence = self.module_index.lookup(prefix)
            if not candidates:
                continue
            if len(candidates) != 1:
                return ResolvedTarget(
                    None, None, EdgeConfidence.LOW, f"ambiguous module prefix {prefix}"
                )
            target_file = candidates[0]
            remainder = parts[split]
            matches = self.by_file_name.get(target_file.id, {}).get(remainder, [])
            if len(matches) == 1:
                return ResolvedTarget(
                    matches[0]["id"],
                    target_file.id,
                    confidence or EdgeConfidence.MEDIUM,
                    f"{remainder} in module {prefix}",
                )
            module_symbol = self.module_symbol_by_file.get(target_file.id)
            return ResolvedTarget(
                module_symbol["id"] if module_symbol else None,
                target_file.id,
                EdgeConfidence.MEDIUM,
                f"module {prefix}",
            )
        return UNRESOLVED

    def _resolve_global(self, name: str, source_file_id: str) -> ResolvedTarget:
        # A bare name with no local definition or import only works because of a
        # star import. Without one, a project-wide unique name is a coincidence,
        # not evidence.
        if source_file_id not in self.star_import_files:
            return UNRESOLVED
        candidates = self.global_callables.get(name, [])
        if len(candidates) == 1:
            return ResolvedTarget(
                candidates[0]["id"],
                candidates[0]["file_id"],
                EdgeConfidence.MEDIUM,
                "unique global name",
            )
        if candidates:
            paths = {candidate["path"] for candidate in candidates}
            if len(paths) == 1:
                return ResolvedTarget(
                    candidates[0]["id"],
                    candidates[0]["file_id"],
                    EdgeConfidence.MEDIUM,
                    "same-file definitions only",
                )
        return UNRESOLVED

    # ----------------------------------------------------------------- helpers
    def _path(self, file_id: str) -> str | None:
        file = self.file_by_id.get(file_id)
        return file.path if file else None


__all__ = [
    "EdgeResolver",
    "ModuleIndex",
    "ResolvedTarget",
    "absolute_module",
    "module_keys",
    "clean_target_name",
]
