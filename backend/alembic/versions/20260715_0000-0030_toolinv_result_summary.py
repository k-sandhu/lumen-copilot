"""tool_invocations.result_summary — the persisted "what it returned" line (#377).

One nullable column on the existing ``tool_invocations`` trace table
(ADR-0008 §4 single-head invariant — ``down_revision`` is the current head
``0029_user_settings``):

* ``tool_invocations.result_summary`` — a **nullable** ``VARCHAR(300)``. NULL ⇒
  the tool handler produced no summary (e.g. a governance denial); a non-null
  value is the short, user-safe result line our own handlers already emit on the
  WS trace (e.g. ``"3 passages"``, ``"13 documents"``). Never raw arguments,
  raw payloads, or vendor error text — the args stay hashed (spec 0004 §2.4) and
  the repository truncates at the write chokepoint.

``tool_invocations`` already carries its tenant RLS backstop from its creating
migration; adding a column does not change the row policy (mirroring the ``0029``
column-add steps), so no policy change is needed here.

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): ``downgrade``
drops the column, restoring the pre-feature shape. Plain ``add_column`` /
``drop_column`` so it renders offline; the live apply runs against a disposable
throwaway database (never the app DB — the #70 lesson).

Revision ID: 0030_toolinv_result_summary
Revises: 0029_user_settings
Create Date: 2026-07-15 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_toolinv_result_summary"
down_revision: str | None = "0029_user_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_invocations",
        sa.Column("result_summary", sa.String(length=300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tool_invocations", "result_summary")
