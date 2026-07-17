"""llm_usage.context_prompt_tokens — final-turn window occupancy (#434 NEW-1)

``prompt_tokens`` sums every completion turn of an answer's loop (plus the
suggestions nicety) — the right number for BILLING, the wrong one for a
context-window meter: a three-search answer "bills" 3× prompts while the window
only ever held the final turn's. This nullable column records the LAST
answer-loop turn's prompt size (suggestions excluded) — what the model's window
actually contained when the answer was produced. NULL on legacy rows and when a
provider reported no usage; readers fall back to ``prompt_tokens`` honestly.

Additive, no index (read via its session's rows), no RLS work (``llm_usage`` is
already tenant-scoped with the 0007 backstop policy).

Reversible (backend/AGENTS.md): ``downgrade`` drops the column.

Revision ID: 0034_llm_usage_ctx
Revises: 0033_message_question
Create Date: 2026-07-17 01:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034_llm_usage_ctx"
down_revision: str | None = "0033_message_question"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column("context_prompt_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "context_prompt_tokens")
