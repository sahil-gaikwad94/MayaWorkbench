"""Provider-specific JSON Schema projection.

Native structured-output modes accept only a subset of JSON Schema, and each
provider has different rules:

* OpenAI ``strict`` mode requires every object to set
  ``additionalProperties: false`` and list **all** properties in ``required``;
* Gemini accepts an OpenAPI-style subset with upper-case type names and no
  ``additionalProperties``.

Sending the raw Pydantic schema therefore produces avoidable HTTP 400s. These
functions produce a deterministic projection and are pure (no I/O, no model
call). Local ``$ref`` pointers are inlined first so neither provider receives a
reference it may reject.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: Keys that describe Python-side defaults rather than the wire contract.
_DROP_KEYS = {
    "$schema",
    "$id",
    "default",
    "examples",
    "discriminator",
    "deprecated",
}


class SchemaProjectionError(ValueError):
    """The schema cannot be projected safely for this provider."""


def resolve_refs(schema: Any, *, max_depth: int = 24) -> Any:
    """Inline local ``$defs``/``definitions`` references.

    External references and reference cycles are refused: silently dropping
    them would send a weaker contract than the caller asked for.
    """
    root = deepcopy(schema)

    def defs_of(document: Any) -> dict[str, Any]:
        if not isinstance(document, dict):
            return {}
        merged: dict[str, Any] = {}
        for key in ("$defs", "definitions"):
            section = document.get(key)
            if isinstance(section, dict):
                merged.update(section)
        return merged

    definitions = defs_of(root)

    def resolve(node: Any, depth: int) -> Any:
        if depth > max_depth:
            raise SchemaProjectionError("schema reference nesting is too deep")
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str):
            if not reference.startswith("#/"):
                raise SchemaProjectionError(
                    f"external schema reference {reference!r} is not supported"
                )
            target: Any = root
            for raw_part in reference[2:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or part not in target:
                    if part in definitions:
                        target = definitions[part]
                        continue
                    raise SchemaProjectionError(
                        f"unresolvable schema reference {reference!r}"
                    )
                target = target[part]
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            resolved = resolve(target, depth + 1)
            if siblings:
                if not isinstance(resolved, dict):
                    raise SchemaProjectionError(
                        f"reference {reference!r} does not resolve to an object"
                    )
                resolved = {**resolved, **resolve(siblings, depth + 1)}
            return resolved
        return {key: resolve(value, depth + 1) for key, value in node.items()}

    projected = resolve(root, 0)
    if isinstance(projected, dict):
        projected.pop("$defs", None)
        projected.pop("definitions", None)
    return projected


def openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a schema for ``response_format.type == "json_schema", strict``.

    OpenAI's strict mode is not ordinary JSON Schema: omitting a property from
    ``required`` or leaving ``additionalProperties`` open is rejected at request
    time. The projection makes every declared property required and closes
    every object. Unsupported annotation keywords are removed.
    """
    resolved = resolve_refs(schema)
    if not isinstance(resolved, dict):
        raise SchemaProjectionError("the response schema must be a JSON object")

    def project(node: Any) -> Any:
        if isinstance(node, list):
            return [project(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP_KEYS:
                continue
            if key == "additionalProperties":
                continue  # closed below for objects
            if key in ("properties", "$defs", "definitions"):
                continue
            if key == "required":
                continue
            result[key] = project(value)
        properties = node.get("properties")
        is_object = node.get("type") == "object" or isinstance(properties, dict)
        if is_object:
            result["type"] = "object"
            projected_properties = (
                {name: project(value) for name, value in properties.items()}
                if isinstance(properties, dict)
                else {}
            )
            result["properties"] = projected_properties
            # Strict mode requires every property to be required. Optional
            # fields remain optional by type (e.g. null in a union) rather than
            # by omission.
            result["required"] = sorted(projected_properties)
            result["additionalProperties"] = False
        return result

    projected = project(resolved)
    if not isinstance(projected, dict):
        raise SchemaProjectionError("the response schema must be a JSON object")
    return projected


_GEMINI_TYPES = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "null": "NULL",
}

_GEMINI_KEEP = {
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "anyOf",
}


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a schema into Gemini's documented OpenAPI subset.

    Gemini has no ``additionalProperties`` and uses upper-case type names. The
    projection resolves local references, drops keywords Gemini does not
    accept, and refuses a schema whose required shape cannot be represented.
    """
    resolved = resolve_refs(schema)
    if not isinstance(resolved, dict):
        raise SchemaProjectionError("the response schema must be a JSON object")

    def project(node: Any) -> Any:
        if isinstance(node, list):
            return [project(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        raw_type = node.get("type")
        nullable = bool(node.get("nullable"))
        if isinstance(raw_type, list):
            non_null = [item for item in raw_type if item != "null"]
            if len(non_null) != len(raw_type):
                nullable = True
            if len(non_null) == 1:
                raw_type = non_null[0]
            elif len(non_null) > 1:
                # Gemini models a union as anyOf.
                result["anyOf"] = [project({"type": item}) for item in non_null]
                raw_type = None
        if isinstance(raw_type, str):
            mapped = _GEMINI_TYPES.get(raw_type.lower())
            if mapped is None:
                raise SchemaProjectionError(f"Gemini does not support type {raw_type!r}")
            result["type"] = mapped
        if nullable:
            result["nullable"] = True
        for key, value in node.items():
            if key in {"type", "nullable", "additionalProperties", "required", "enum"}:
                continue
            if key in _GEMINI_KEEP:
                result[key] = project(value)
        properties = node.get("properties")
        if isinstance(properties, dict):
            result["properties"] = {
                name: project(value) for name, value in properties.items()
            }
            required = node.get("required")
            if isinstance(required, list):
                result["required"] = [str(item) for item in required]
        enum = node.get("enum")
        if isinstance(enum, list) and enum:
            if all(isinstance(item, str) for item in enum):
                result["enum"] = list(enum)
            else:
                # Gemini enum values are strings; a numeric enum would be
                # rejected, so leave the scalar type unconstrained.
                result.pop("enum", None)
        return result

    projected = project(resolved)
    if not isinstance(projected, dict):
        raise SchemaProjectionError("the response schema must be a JSON object")
    return projected


__all__ = [
    "SchemaProjectionError",
    "resolve_refs",
    "openai_strict_schema",
    "gemini_schema",
]
