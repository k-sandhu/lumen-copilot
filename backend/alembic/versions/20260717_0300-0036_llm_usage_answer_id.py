"""llm_usage.answer_id — durable answer correlation for route scopes (#413, #440 NEW-1)

Per-route usage attribution (ADR-0016 §2.6) writes one ``llm_usage`` row per
model route scope of an answer: only the winning scope carries ``message_id``;
failed/superseded scopes — and error-path salvage rows whose answer never
produced a message at all — are message-less. ``answer_id`` (the pre-minted
assistant message id, deliberately NOT an FK: the message row may not exist)
groups every scope of one answer so the ledger can reconstruct
``(answer, route-scope)`` durably.

Additive, nullable, no index (grouping reads are per-session over few rows), no
RLS work (``llm_usage`` already carries the 0007-style backstop policy).

Reversible (backend/AGENTS.md): ``downgrade`` drops the column.

Revision ID: 0036_llm_usage_answer
Revises: 0035_tenant_fallbacks
Create Date: 2026-07-17 03:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036_llm_usage_answer"
down_revision: str | None = "0035_tenant_fallbacks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("answer_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "answer_id")
