"""Tool argument schemas stay portable and are enforced before handler I/O."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditActor
from app.domain.entities import Role
from app.domain.llm import ToolCall
from app.domain.tools import ERROR_BAD_ARGS, RiskTier, ToolHandlerResult
from app.llm.gateway import LLMGateway
from app.services.audit import AuditSink
from app.services.tools import runner as runner_module
from app.services.tools.registry import UnknownToolError, get_tool, tool_specs
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ToolContext, ToolDefinition

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


def _complex_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            "mode": {"type": "string", "enum": ["brief", "full"]},
            "filters": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["field"],
                },
            },
        },
        "required": ["query"],
    }


def _assert_strict_object(schema: dict[str, Any]) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    for _name, child in schema["properties"].items():
        if isinstance(child, dict) and child.get("type") == "object":
            _assert_strict_object(child)
        elif isinstance(child, dict) and "anyOf" in child:
            branches = child["anyOf"]
            assert any(branch.get("type") == "null" for branch in branches)
            for branch in branches:
                if branch.get("type") == "object":
                    _assert_strict_object(branch)
                if branch.get("type") == "array":
                    item = branch["items"]
                    if item.get("type") == "object":
                        _assert_strict_object(item)
        elif isinstance(child, dict) and child.get("type") == "array":
            item = child["items"]
            if item.get("type") == "object":
                _assert_strict_object(item)


def test_strict_schema_closes_nested_objects_and_makes_optional_properties_nullable() -> None:
    from app.domain.tool_schema import strict_schema

    schema = _complex_schema()
    before = copy.deepcopy(schema)

    projected = strict_schema(schema)

    assert schema == before  # projection never mutates the source definition
    _assert_strict_object(projected)
    assert "query" in projected["required"]
    assert {"limit", "mode", "filters"} <= set(projected["required"])
    assert any(
        branch.get("type") == "null" for branch in projected["properties"]["filters"]["anyOf"]
    )


def test_validate_arguments_accepts_legacy_omissions_and_valid_nested_values() -> None:
    from app.domain.tool_schema import validate_arguments

    schema = _complex_schema()
    assert validate_arguments({"query": "find"}, schema) is None
    assert (
        validate_arguments(
            {
                "query": "find",
                "limit": 3,
                "mode": "brief",
                "filters": [{"field": "status", "values": ["open"]}],
            },
            schema,
        )
        is None
    )


@pytest.mark.parametrize(
    ("arguments", "expected_path", "expected_keyword"),
    [
        ({"query": 4}, "query", "type"),
        ({"query": "x", "limit": 6}, "limit", "maximum"),
        ({"query": "x", "mode": "verbose"}, "mode", "enum"),
        ({"query": "x", "extra": True}, "extra", "additionalProperties"),
        ({"query": "x", "filters": [{"values": ["v"]}]}, "filters", "required"),
        (
            {"query": "x", "filters": [{"field": "f", "values": [3]}]},
            "filters",
            "type",
        ),
    ],
    ids=[
        "wrong-type",
        "numeric-bound",
        "enum",
        "unknown-property",
        "nested-required",
        "nested-array-type",
    ],
)
def test_validate_arguments_rejects_invalid_values_with_actionable_path(
    arguments: dict[str, Any], expected_path: str, expected_keyword: str
) -> None:
    from app.domain.tool_schema import validate_arguments

    error = validate_arguments(arguments, _complex_schema())

    assert error is not None
    assert expected_path in error
    if expected_keyword == "additionalProperties":
        assert "unknown" in error.lower() or "additionalproperties" in error.lower()
    else:
        assert expected_keyword.lower() in error.lower()


def test_validate_arguments_supports_local_refs_and_rejects_remote_refs() -> None:
    from app.domain.tool_schema import validate_arguments

    local_schema = {
        "type": "object",
        "properties": {"detail": {"$ref": "#/$defs/detail"}},
        "required": ["detail"],
        "$defs": {
            "detail": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            }
        },
    }
    assert validate_arguments({"detail": {"code": "ok"}}, local_schema) is None
    remote_schema = {
        "type": "object",
        "properties": {"detail": {"$ref": "https://example.invalid/schema.json"}},
    }
    error = validate_arguments({"detail": {}}, remote_schema)
    assert error is not None
    assert "remote" in error.lower() or "unsupported" in error.lower()


def test_required_nullable_property_remains_distinct_from_optional_property() -> None:
    from app.domain.tool_schema import strict_schema, validate_arguments

    schema = {
        "type": "object",
        "properties": {
            "required_value": {"type": ["string", "null"]},
            "optional_value": {"type": "string"},
        },
        "required": ["required_value"],
    }

    projected = strict_schema(schema)

    assert validate_arguments({"required_value": None}, schema) is None
    assert "required_value" in projected["required"]
    assert "optional_value" in projected["required"]
    assert validate_arguments({"optional_value": "x"}, schema) is not None


def test_gateway_offers_strict_portable_schema_without_mutating_native_schema() -> None:
    definition = get_tool("search_documents")
    original = copy.deepcopy(definition.json_schema)
    wire = LLMGateway._to_wire_tools(tool_specs([definition.name]))
    projected = wire[0]["function"]["parameters"]

    assert wire[0]["function"] == {
        "name": definition.name,
        "description": definition.description,
        "parameters": projected,
    }
    assert definition.json_schema == original
    assert projected["additionalProperties"] is False
    assert set(projected["required"]) == set(projected["properties"])
    assert not any(key.startswith("x-") for key in projected)


def test_builtin_tool_schemas_are_closed_and_document_every_parameter() -> None:
    from app.services.tools.registry import get_tool

    def visit(schema: dict[str, Any], path: str) -> None:
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, path
            for name, child in schema.get("properties", {}).items():
                assert child.get("description"), f"{path}.{name} needs a description"
                visit(child, f"{path}.{name}")
        if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            visit(schema["items"], f"{path}[]")

    names = (
        "search_text",
        "search_documents",
        "list_documents",
        "get_document",
        "ask_user",
        "write_file",
        "run_python",
        "web_search",
    )
    for name in names:
        definition = get_tool(name)
        assert definition.description.strip(), name
        visit(definition.json_schema, name)


def test_builtin_schema_bounds_match_handler_limits_and_describe_defaults() -> None:
    from app.domain.chat import (
        ASK_USER_MAX_DESCRIPTION_CHARS,
        ASK_USER_MAX_LABEL_CHARS,
        ASK_USER_MAX_OPTIONS,
        ASK_USER_MAX_QUESTION_CHARS,
        ASK_USER_MIN_OPTIONS,
    )
    from app.services.tools.types import ToolContext

    ask_schema = get_tool("ask_user").json_schema["properties"]
    assert ask_schema["question"]["minLength"] == 1
    assert ask_schema["question"]["maxLength"] == ASK_USER_MAX_QUESTION_CHARS
    assert ask_schema["options"]["minItems"] == ASK_USER_MIN_OPTIONS
    assert ask_schema["options"]["maxItems"] == ASK_USER_MAX_OPTIONS
    assert "2-4" in ask_schema["options"]["description"]
    option_schema = ask_schema["options"]["items"]["properties"]
    assert option_schema["label"]["minLength"] == 1
    assert option_schema["label"]["maxLength"] == ASK_USER_MAX_LABEL_CHARS
    assert option_schema["description"]["maxLength"] == ASK_USER_MAX_DESCRIPTION_CHARS

    search_text = get_tool("search_text")
    default_k = ToolContext.__dataclass_fields__["default_k"].default
    assert search_text.json_schema["properties"]["k"]["default"] == default_k
    assert str(default_k) in search_text.json_schema["properties"]["k"]["description"]

    search_documents = get_tool("search_documents").json_schema["properties"]
    assert search_documents["name_or_query"]["minLength"] == 1
    assert search_documents["k"]["default"] == 10
    assert "10" in search_documents["k"]["description"]
    list_limit = get_tool("list_documents").json_schema["properties"]["k"]
    assert list_limit["maximum"] == 50
    assert list_limit["default"] == 50

    packages = get_tool("run_python").json_schema["properties"]["packages"]
    assert packages["maxItems"] == 50
    assert "50" in packages["description"] and "default" in packages["description"].lower()


@pytest.mark.parametrize("extra_value", [None, "unexpected"])
def test_unknown_properties_are_rejected_even_when_null(extra_value: Any) -> None:
    from app.domain.tool_schema import validate_arguments

    error = validate_arguments({"query": "x", "extra": extra_value}, _complex_schema())

    assert error is not None
    assert "extra" in error


def test_bad_local_reference_is_reported_without_attempting_remote_resolution() -> None:
    from app.domain.tool_schema import validate_arguments

    schema = {
        "type": "object",
        "properties": {"detail": {"$ref": "#/$defs/missing"}},
        "required": ["detail"],
        "$defs": {},
    }

    error = validate_arguments({"detail": {}}, schema)

    assert error is not None
    assert "ref" in error.lower() or "reference" in error.lower()


class _World:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self.session = session
        self.principal = principal


class _FakeRetrieval:
    pass


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Schema Test")
            user = await UserRepository(session, tenant.id).create(
                email="schema@test.invalid", password_hash="x", roles=[Role.MEMBER]
            )
            await session.commit()
            yield _World(
                session,
                Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
            )
    finally:
        await engine.dispose()


def _schema_runner(
    world: _World,
    definition: ToolDefinition,
    monkeypatch: pytest.MonkeyPatch,
) -> ToolRunner:
    def resolve(name: str) -> ToolDefinition:
        if name == definition.name:
            return definition
        raise UnknownToolError(name)

    monkeypatch.setattr(runner_module, "get_tool", resolve)
    tenant_id = world.principal.tenant_id
    return ToolRunner(
        allowed=frozenset({definition.name}),
        invocations=ToolInvocationRepository(world.session, tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, tenant_id)),
        actor=AuditActor.user(world.principal.user_id),
        request_id="schema-test",
        source_ip="127.0.0.1",
    )


async def test_runner_rejects_bad_arguments_before_calling_handler(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        calls.append(args)
        return ToolHandlerResult(content="ran")

    definition = ToolDefinition(
        name="schema_probe",
        description="schema check",
        json_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "required_nullable": {"type": ["string", "null"]},
                "optional": {"type": "string"},
            },
            "required": ["query", "required_nullable"],
        },
        handler=handler,
        risk_tier=RiskTier.T0,
        read_only=True,
    )
    runner = _schema_runner(world, definition, monkeypatch)
    invalid = [
        {"query": "x", "limit": 9},
        {"query": "x", "unexpected": "value"},
        {"query": 9},
    ]

    results = [
        await runner.run(
            call=ToolCall(id=f"bad-{index}", name=definition.name, arguments=args),
            context=ToolContext(principal=world.principal, retrieval=_FakeRetrieval()),  # type: ignore[arg-type]
        )
        for index, args in enumerate(invalid)
    ]

    assert calls == []
    assert all(result.ok is False and result.error == ERROR_BAD_ARGS for result in results)


async def test_runner_strips_optional_nulls_before_legacy_handler(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        calls.append(dict(args))
        return ToolHandlerResult(content="ran", payload={"args": args})

    definition = ToolDefinition(
        name="schema_probe",
        description="schema check",
        json_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "required_nullable": {"type": ["string", "null"]},
                "optional": {"type": "string"},
                "nested": {"$ref": "#/$defs/nested"},
            },
            "required": ["query", "required_nullable", "nested"],
            "$defs": {
                "nested": {
                    "type": "object",
                    "properties": {"optional": {"type": "string"}},
                }
            },
        },
        handler=handler,
    )
    runner = _schema_runner(world, definition, monkeypatch)
    result = await runner.run(
        call=ToolCall(
            id="null-optionals",
            name=definition.name,
            arguments={
                "query": "x",
                "required_nullable": None,
                "optional": None,
                "nested": {"optional": None},
            },
        ),
        context=ToolContext(principal=world.principal, retrieval=_FakeRetrieval()),  # type: ignore[arg-type]
    )

    assert result.ok is True
    assert calls == [{"query": "x", "required_nullable": None, "nested": {}}]
