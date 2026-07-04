"""assistant library governance — governance columns on assistants (#217)

The persistence half of admin library governance (issue #217, E6-6/E6-8): the
``assistants`` head row gains an **orthogonal** governance axis an admin manages,
separate from the owner-managed lifecycle ``status`` and CRUD. Four additive
columns:

* ``certification_state`` — the admin certification verdict (``none`` |
  ``certified`` | ``deprecated``), CHECK-pinned like the other enum columns so a
  bad value can never be stored. Backfilled ``none`` for existing rows.
* ``featured`` — whether an admin has featured/pinned the assistant in the
  library. Backfilled ``false``.
* ``category`` — a nullable free-text library grouping label.
* ``disabled_at`` — set (in lockstep with ``status=disabled``) when an admin
  disables the assistant, so the existing "only a published assistant may start"
  gate (chat / schedule / run) refuses it. Nullable ⇒ not disabled.

All four are **additive** — an existing assistant is untouched behaviourally
(``none`` / ``false`` / ``null`` / ``null``). No new table, so no new RLS policy is
needed: the ``assistants`` table already carries the ``0016`` RLS backstop that
covers every column. ``down_revision`` is the current head ``0022_sandbox_policy``
(single-head invariant, ADR-0008 §4).

Reversible (backend/AGENTS.md): ``downgrade`` drops the CHECK constraint and the
four columns, restoring the pre-#217 shape. Offline DDL render asserts the shape;
the live apply runs against a disposable throwaway database (the #70 lesson).

Revision ID: 0023_assistant_governance
Revises: 0022_sandbox_policy
Create Date: 2026-07-03 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_assistant_governance"
down_revision: str | None = "0022_sandbox_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Certification verdict — server_default backfills existing rows to 'none',
    # then the column is NOT NULL (no existing assistant is left uncertified-null).
    op.add_column(
        "assistants",
        sa.Column(
            "certification_state",
            sa.String(length=20),
            nullable=False,
            server_default="none",
        ),
    )
    # Featured flag — server_default backfills existing rows to false.
    op.add_column(
        "assistants",
        sa.Column(
            "featured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Library category label — nullable (uncategorised by default).
    op.add_column(
        "assistants",
        sa.Column("category", sa.String(length=100), nullable=True),
    )
    # When an admin disabled the assistant — null ⇒ not disabled.
    op.add_column(
        "assistants",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Pin the certification enum domain at the DB so a bad value can never be
    # stored — the same posture as ck_assistants_status / ck_assistants_autonomy_level.
    op.create_check_constraint(
        "ck_assistants_certification_state",
        "assistants",
        "certification_state in ('none', 'certified', 'deprecated')",
    )


def downgrade() -> None:
    # Reverse order: drop the CHECK first, then the four columns.
    op.drop_constraint(
        "ck_assistants_certification_state", "assistants", type_="check"
    )
    op.drop_column("assistants", "disabled_at")
    op.drop_column("assistants", "category")
    op.drop_column("assistants", "featured")
    op.drop_column("assistants", "certification_state")
