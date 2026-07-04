"""tenants.logo_key — per-tenant application logo (admin branding)

The single schema step for the per-tenant application-logo feature: a tenant
ADMIN uploads a brand mark that replaces the default "Lumen / Copilot" wordmark
in the app shell for every user of that tenant. It adds exactly one column to
``tenants`` (ADR-0008 §4 single-head invariant — ``down_revision`` is the current
head ``0022_sandbox_policy``):

* ``logo_key`` — a **nullable** ``VARCHAR(1000)``. NULL ⇒ the tenant uses the
  default brand mark; a non-null value is the object-store key of the tenant's
  uploaded logo (stored via the same ObjectStore as uploads/artifacts). The shell
  reads a presigned GET URL derived from this key off ``GET /auth/me``.

The column lives on the parent ``tenants`` table, which carries no ``tenant_id``
and is deliberately outside the RLS backstop (0007), so no policy change is needed
(mirroring the ``0012`` ``max_tool_turns`` step). No default, no check — a logo
key is either present or absent.

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): ``downgrade``
drops the column, restoring the pre-feature shape. It is a plain ``add_column`` so
it renders offline; the live apply runs against a disposable throwaway database
(never the app DB — the #70 lesson).

Revision ID: 0027_tenant_logo
Revises: 0026_toolinv_msg_fk_deferrable
Create Date: 2026-07-03 01:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027_tenant_logo"
down_revision: str | None = "0026_toolinv_msg_fk_deferrable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("logo_key", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "logo_key")
