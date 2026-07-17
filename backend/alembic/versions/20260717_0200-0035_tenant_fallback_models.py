"""tenants.fallback_models — ordered turn-failover model list (ADR-0016 §4, #413)

When an answer's model exhausts its bounded retry budget on transient provider
faults, the runtime fails over down this per-tenant ordered list of model ids.
NULL/empty ⇒ no fallback (the pre-#413 behavior: the answer errors). The list
is validated at the admin write path exactly like a send-path model id, so the
column stores only ids the tenant could have chosen directly.

Additive, nullable, no index (read once per answer via the tenant row), no RLS
work (``tenants`` is the tenant-anchor table with its 0007 policy).

Reversible (backend/AGENTS.md): ``downgrade`` drops the column.

Revision ID: 0035_tenant_fallbacks
Revises: 0034_llm_usage_ctx
Create Date: 2026-07-17 02:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0035_tenant_fallbacks"
down_revision: str | None = "0034_llm_usage_ctx"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("tenants", sa.Column("fallback_models", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "fallback_models")
