"""Pure, provider-portable tool schema projection and argument validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry


def _closed(schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    for key in ("$defs", "definitions", "properties", "patternProperties"):
        if isinstance(result.get(key), dict):
            result[key] = {
                name: _closed(value) if isinstance(value, dict) else value
                for name, value in result[key].items()
            }
    for key in ("items", "not", "if", "then", "else"):
        if isinstance(result.get(key), dict):
            result[key] = _closed(result[key])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [
                _closed(value) if isinstance(value, dict) else value for value in result[key]
            ]
    if result.get("type") == "object" or "properties" in result:
        result["additionalProperties"] = False
    return result


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Close objects and project omitted optional fields as required nullable fields.

    Does not enable provider-native strict mode or mutate local omission defaults.
    """
    result = _closed(schema)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            node.pop("default", None)
            for value in list(node.values()):
                visit(value)
            properties = node.get("properties")
            if node.get("type") == "object" or isinstance(properties, dict):
                properties = properties or {}
                required = set(node.get("required", []))
                for name, value in list(properties.items()):
                    if name not in required:
                        properties[name] = {"anyOf": [value, {"type": "null"}]}
                node["properties"] = properties
                node["required"] = list(properties)
                node["additionalProperties"] = False

    visit(result)
    return result


def _remote_reference(node: Any) -> bool:
    if isinstance(node, list):
        return any(_remote_reference(value) for value in node)
    if not isinstance(node, dict):
        return False
    for key in ("$ref", "$dynamicRef"):
        ref = node.get(key)
        if isinstance(ref, str) and not ref.startswith("#"):
            return True
    return any(_remote_reference(value) for value in node.values())


def legacy_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Treat provider nulls for optional parameters as the legacy omission default."""
    return _legacy_arguments(arguments, schema, schema)


def _local_schema(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve local JSON pointers without retrieving external schema resources."""
    visited: set[str] = set()
    while isinstance(ref := node.get("$ref"), str) and ref.startswith("#/"):
        if ref in visited:
            break
        visited.add(ref)
        target: Any = root
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            break
        node = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
    return node


def _legacy_arguments(
    arguments: dict[str, Any], schema: dict[str, Any], root: dict[str, Any]
) -> dict[str, Any]:
    schema = _local_schema(schema, root)
    result = deepcopy(arguments)
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return result
    required = set(schema.get("required", []))
    for name, value in list(result.items()):
        child = properties.get(name)
        if value is None and name in properties and name not in required:
            result.pop(name)
        elif isinstance(child, dict) and isinstance(value, dict):
            result[name] = _legacy_arguments(value, child, root)
        elif isinstance(child, dict) and isinstance(value, list):
            items = child.get("items")
            if isinstance(items, dict):
                result[name] = [
                    _legacy_arguments(item, items, root) if isinstance(item, dict) else item
                    for item in value
                ]
    return result


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Return an actionable field path and constraint, never raw values or I/O."""
    if _remote_reference(schema):
        return (
            "Tool schema uses an unsupported remote reference; ask an administrator to repair it."
        )
    closed = _closed(schema)
    try:
        Draft202012Validator.check_schema(closed)
        errors = list(Draft202012Validator(closed, registry=Registry()).iter_errors(arguments))
    except SchemaError:
        return "Tool schema is invalid; ask an administrator to repair it."
    except Exception:
        # Broken local references (or other invalid provider schema metadata)
        # are tool configuration errors, never an answer-stream exception.
        return "Tool schema reference is invalid; ask an administrator to repair it."
    if not errors:
        return None
    error = min(
        errors, key=lambda item: (tuple(str(p) for p in item.absolute_path), item.validator)
    )
    path = ".".join(str(part) for part in error.absolute_path) or "arguments"
    constraint = error.validator
    if constraint == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        detail = f"provide required field(s) {', '.join(missing)}"
    elif constraint == "additionalProperties":
        extras = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        if extras:
            path += "." + str(extras[0])[:80]
        detail = "remove unknown properties; use only the advertised fields"
    elif constraint in {
        "type",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }:
        detail = f"must satisfy {constraint} {error.validator_value}"
    else:
        detail = f"must satisfy {constraint}; check the advertised schema"
    return f"Invalid {path}: {detail}. Correct the arguments and retry."
