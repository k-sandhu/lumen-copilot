"""Reload, isolation, and wire regressions for server-built answer limits (#833)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import update

from app.api.v1.chat import _message_to_response
from app.db import models
from app.db.repositories import MessageRepository
from app.domain.entities import MessageRole
from app.services.chat_service import MessageView
from tests.test_chat_runtime import _Ctx
from tests.test_chat_runtime import ctx as _runtime_ctx

ctx = _runtime_ctx

_KINDS = ("tool_turns", "tool_calls", "external_calls", "input_tokens", "output_tokens", "cost")


@pytest.mark.parametrize("kind", _KINDS)
async def test_limit_round_trips_to_reload_dto_without_tenant_leak(ctx: _Ctx, kind: str) -> None:
    message_id = uuid4()
    async with ctx.sessionmaker() as session:
        await MessageRepository(session, ctx.tenant_id).add_with_id(
            message_id=message_id,
            session_id=ctx.session_id,
            role=MessageRole.ASSISTANT,
            content="Answer from permitted evidence.",
            limit_kind=kind,
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        own = MessageRepository(session, ctx.tenant_id)
        stored = await own.get(message_id)
        assert stored is not None and stored.limit_kind == kind
        dto = _message_to_response(MessageView(message=stored, citations=[]))
        payload = dto.model_dump(mode="json")["limit"]
        assert payload is not None
        assert payload["kind"] == kind
        assert payload["message"].endswith(".")
        assert not any(char.isdigit() for char in payload["message"])
        assert "Answer from permitted evidence" not in payload["message"]
        assert await MessageRepository(session, uuid4()).get(message_id) is None
        assert await MessageRepository(session, uuid4()).list_for_session(ctx.session_id) == []


async def test_normal_and_unknown_stored_limit_render_no_disclosure(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        row = await MessageRepository(session, ctx.tenant_id).add_with_id(
            message_id=uuid4(),
            session_id=ctx.session_id,
            role=MessageRole.ASSISTANT,
            content="Ordinary answer.",
        )
        assert _message_to_response(MessageView(row, [])).model_dump()["limit"] is None
        # A malformed legacy value must never become a model-written UI note.
        await session.execute(
            update(models.Message).where(models.Message.id == row.id).values(limit_kind="hostile")
        )
        await session.commit()
        stored = await MessageRepository(session, ctx.tenant_id).get(row.id)
        assert stored is not None
        assert _message_to_response(MessageView(stored, [])).model_dump()["limit"] is None


def test_limit_contract_accepts_only_known_kinds_and_one_sentence_field() -> None:
    contract = json.loads(
        (Path(__file__).parents[2] / "contracts/websocket-envelopes.schema.json").read_text()
    )
    validator = Draft202012Validator({"$ref": "#/$defs/ChatDoneData", "$defs": contract["$defs"]})
    ordinary = {"messageId": str(uuid4()), "finishReason": "stop", "citationCount": 0}
    validator.validate(ordinary)
    validator.validate({**ordinary, "limit": None})
    for kind in _KINDS:
        validator.validate({**ordinary, "limit": {"kind": kind, "message": "Research stopped."}})
    for invalid in (
        {"kind": "invented", "message": "Research stopped."},
        {"kind": "tool_calls"},
        {"kind": "cost", "message": "Research stopped.", "modelText": "unsafe"},
    ):
        with pytest.raises(ValidationError):
            validator.validate({**ordinary, "limit": invalid})


def test_answer_limit_migration_is_nullable_and_reversible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0054_billed_cost:0055_answer_limit", sql=True)
    up = capsys.readouterr().out.lower()
    assert "alter table messages add column limit_kind varchar(32);" in up
    command.downgrade(cfg, "0055_answer_limit:0054_billed_cost", sql=True)
    assert "alter table messages drop column limit_kind;" in capsys.readouterr().out.lower()
