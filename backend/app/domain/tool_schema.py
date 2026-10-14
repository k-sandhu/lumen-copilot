"""Pure, provider-portable tool schema projection and argument validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry


def _object_schema(schema: dict[str, Any]) -> bool:
    types = schema.get("type", [])
    return (
        types == "object"
        or (isinstance(types, list) and "object" in types)
        or "properties" in schema
    )


def _composed(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Hoist unconditional allOf property constraints to their shared object.

    Closing separate fragments would forbid their siblings' declared fields.
    Intersections on a shared property remain intersections, not last-wins.
    Non-property constraints remain in allOf and explicit fragment closures
    retain their original scope.
    """
    result = deepcopy(schema)
    if not isinstance(result.get("allOf"), list):
        return result
    properties = dict(result.get("properties", {}))
    required = list(result.get("required", []))
    fragments: list[Any] = []
    object_fragment = False
    for branch in result["allOf"]:
        if not isinstance(branch, dict):
            fragments.append(branch)
            continue
        fragment = _composed(_local_schema(branch, root), root)
        object_fragment = object_fragment or _object_schema(fragment)
        for name, child in fragment.get("properties", {}).items():
            if name in properties:
                properties[name] = {"allOf": [properties[name], child]}
            else:
                properties[name] = child
        required.extend(name for name in fragment.get("required", []) if name not in required)
        # Explicit scoped closure/pattern constraints still need their declarations.
        if not any(
            key in fragment
            for key in ("additionalProperties", "patternProperties", "unevaluatedProperties")
        ):
            fragment.pop("properties", None)
            fragment.pop("required", None)
        if fragment:
            fragments.append(fragment)
    if properties or object_fragment:
        result["properties"] = properties
    if required:
        result["required"] = required
    if fragments:
        result["allOf"] = fragments
    else:
        result.pop("allOf")
    return result


def _closed(
    schema: dict[str, Any],
    root: dict[str, Any] | None = None,
    *,
    strict: bool = False,
    close_object: bool = True,
) -> dict[str, Any]:
    root = root if root is not None else schema
    result = _composed(schema, root)
    for key in ("$defs", "definitions", "properties", "patternProperties"):
        if isinstance(result.get(key), dict):
            result[key] = {
                name: _closed(value, root, strict=strict) if isinstance(value, dict) else value
                for name, value in result[key].items()
            }
    for key in ("items", "not", "if", "then", "else", "contains"):
        if isinstance(result.get(key), dict):
            result[key] = _closed(result[key], root, strict=strict)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [
                _closed(value, root, strict=strict, close_object=key != "allOf")
                if isinstance(value, dict)
                else value
                for value in result[key]
            ]
    if strict:
        result.pop("default", None)
    if close_object and _object_schema(result):
        result["additionalProperties"] = False
        if strict:
            properties = result.get("properties", {})
            required = set(result.get("required", []))
            for name, value in list(properties.items()):
                if name not in required:
                    properties[name] = {"anyOf": [value, {"type": "null"}]}
            result["properties"] = properties
            result["required"] = list(properties)
    return result


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Close objects and project omitted optional fields as required nullable fields.

    Does not enable provider-native strict mode or mutate local omission defaults.
    """
    if configuration_error := schema_error(schema):
        raise ValueError(configuration_error)
    try:
        return _closed(schema, strict=True)
    except Exception:
        raise ValueError("Tool schema is invalid; ask an administrator to repair it.") from None


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
    # Leave rejected input intact so the runner can finalize its original hash.
    if schema_error(schema) is not None:
        return deepcopy(arguments)
    try:
        return _legacy_arguments(arguments, schema, schema)
    except Exception:
        # Invalid local references/configuration must reach validate_arguments,
        # which returns a safe repair message through normal audited finalization.
        return deepcopy(arguments)


def schema_error(schema: dict[str, Any]) -> str | None:
    """Check metadata before projection/normalization can interpret it."""
    if _remote_reference(schema):
        return (
            "Tool schema uses an unsupported remote reference; ask an administrator to repair it."
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return "Tool schema is invalid; ask an administrator to repair it."
    return None


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
    result = _legacy_value(arguments, schema, root)
    assert isinstance(result, dict)
    return result


def _legacy_value(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    schema = _composed(_local_schema(schema, root), root)
    result = deepcopy(value)
    if isinstance(result, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, child_value in list(result.items()):
            child = properties.get(name)
            if child_value is None and name in properties and name not in required:
                result.pop(name)
            elif isinstance(child, dict):
                result[name] = _legacy_value(child_value, child, root)
    elif isinstance(result, list):
        items = schema.get("items")
        prefix = schema.get("prefixItems", [])
        for index, item in enumerate(result):
            child = prefix[index] if index < len(prefix) else items
            if isinstance(child, dict):
                result[index] = _legacy_value(item, child, root)
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, []):
            if not isinstance(branch, dict):
                continue
            candidate = _legacy_value(result, branch, root)
            validator = Draft202012Validator(_closed(root), registry=Registry()).evolve(
                schema=_closed(branch, root)
            )
            if validator.is_valid(candidate):
                result = candidate
                break
    return result


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Return an actionable field path and constraint, never raw values or I/O."""
    if configuration_error := schema_error(schema):
        return configuration_error
    try:
        closed = _closed(schema)
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
