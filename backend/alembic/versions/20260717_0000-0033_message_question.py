"""messages.question — the persisted clarifying question (spec 0006, #429)

An assistant turn that ended by asking the user a clarifying question
(``finishReason=ask_user``) stores the question payload — the REST
``AskUserQuestion`` shape verbatim (question text, 2–4 options, allow_free_text)
— so the UI can re-render the clickable options after a reload. Every other turn
leaves it NULL; existing rows are untouched.

Additive, no index (the payload is only ever read via its own message row), and
no RLS work: ``messages`` is already tenant-scoped with the fail-closed ``0007``
backstop policy, which covers new columns automatically.

JSONB on Postgres, generic JSON elsewhere (the SQLite offline tests) — the same
portable variant the ORM's ``_JSON`` type uses.

Reversible (backend/AGENTS.md): ``downgrade`` drops the column.

Revision ID: 0033_message_question
Revises: 0032_llm_usage
Create Date: 2026-07-17 00:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0033_message_question"
down_revision: str | None = "0032_llm_usage"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")  # type: ignore[no-untyped-call]


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("question", _JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "question")
