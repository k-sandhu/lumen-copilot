"""Provider-billed spend per usage scope (#832).

Revision ID: 0054_billed_cost
Revises: 0049_discovery_metadata
"""

import sqlalchemy as sa
from alembic import op

revision = "0054_billed_cost"
down_revision = "0049_discovery_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("cost_usd", sa.Numeric(48, 30), nullable=True))
    op.add_column(
        "llm_usage", sa.Column("unpriced_calls", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_check_constraint("ck_llm_usage_cost_nonneg", "llm_usage", "cost_usd >= 0")
    op.create_check_constraint("ck_llm_usage_unpriced_nonneg", "llm_usage", "unpriced_calls >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_llm_usage_unpriced_nonneg", "llm_usage", type_="check")
    op.drop_constraint("ck_llm_usage_cost_nonneg", "llm_usage", type_="check")
    op.drop_column("llm_usage", "unpriced_calls")
    op.drop_column("llm_usage", "cost_usd")
