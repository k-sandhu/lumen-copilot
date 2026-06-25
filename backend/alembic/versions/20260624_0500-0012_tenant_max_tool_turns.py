"""tenants.max_tool_turns — per-tenant chat tool-turn budget override (#148)

The single M-series schema step for the admin-configurable agent loop bound
(issue #148, ADR-0008 §4 single-head invariant — ``down_revision`` is the current
head ``0011_recent_searches``). It adds exactly one column to ``tenants``:

* ``max_tool_turns`` — a **nullable** integer. NULL ⇒ the tenant uses the system
  default (``Settings.chat_max_tool_turns``); a non-null value is the tenant
  admin's per-tenant override of how many tool-calling turns the grounded answer
  runtime may take before it forces a final synthesis.

A ``CHECK`` (``ck_tenants_max_tool_turns_range``) pins the override to the
supported **1–50** band at the DB, mirroring the wire schema and the ORM
``__table_args__`` — a misconfiguration can neither disable bounding (0/negative)
nor explode answer cost (>50). The column lives on the parent ``tenants`` table,
which carries no ``tenant_id`` and is deliberately outside the RLS backstop
(0007), so no policy change is needed.

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): ``downgrade``
drops the check then the column, restoring the pre-#148 shape. Offline DDL render
asserts the shapes; the live apply runs against a disposable throwaway database
(never the app DB — the #70 lesson).

Revision ID: 0012_tenant_max_tool_turns
Revises: 0011_recent_searches
Create Date: 2026-06-24 05:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_tenant_max_tool_turns"
down_revision: str | None = "0011_recent_searches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("max_tool_turns", sa.Integer(), nullable=True),
    )
    # NULL ⇒ system default; when set it must sit in the supported 1–50 band.
    op.create_check_constraint(
        "ck_tenants_max_tool_turns_range",
        "tenants",
        "max_tool_turns IS NULL OR (max_tool_turns >= 1 AND max_tool_turns <= 50)",
    )


def downgrade() -> None:
    # Reverse order: drop the check, then the column.
    op.drop_constraint("ck_tenants_max_tool_turns_range", "tenants", type_="check")
    op.drop_column("tenants", "max_tool_turns")
