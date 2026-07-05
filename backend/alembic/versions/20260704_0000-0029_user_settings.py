"""user settings — user_preferences.custom_instructions + users.avatar_key

The single schema step for the user settings page: two nullable columns added to
existing tables (ADR-0008 §4 single-head invariant — ``down_revision`` is the
current head ``0028_llm_providers``):

* ``user_preferences.custom_instructions`` — a **nullable** ``TEXT``. NULL ⇒ the
  user has no custom instructions; a non-null value is a per-user free-text preamble
  prepended to the chat system prompt (before the grounding contract; capped at the
  wire, not the DB).
* ``users.avatar_key`` — a **nullable** ``VARCHAR(1000)``. NULL ⇒ the user has no
  profile avatar (the shell renders the initials fallback); a non-null value is the
  object-store key of the user's uploaded avatar. The shell reads a presigned GET
  URL derived from this key off ``GET /auth/me``.

Both are plain ``add_column`` steps on existing tables. ``users`` is the parent
identity table (outside the RLS backstop, like the ``0027`` ``tenants.logo_key``
step); ``user_preferences`` already has its RLS policy (0009) which applies to the
whole row — adding a column does not change the policy. So no policy change is
needed here (mirroring the ``0027`` logo step).

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): ``downgrade``
drops both columns, restoring the pre-feature shape. Plain ``add_column`` /
``drop_column`` so it renders offline; the live apply runs against a disposable
throwaway database (never the app DB — the #70 lesson).

Revision ID: 0029_user_settings
Revises: 0028_llm_providers
Create Date: 2026-07-04 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029_user_settings"
down_revision: str | None = "0028_llm_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("custom_instructions", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("avatar_key", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "avatar_key")
    op.drop_column("user_preferences", "custom_instructions")
