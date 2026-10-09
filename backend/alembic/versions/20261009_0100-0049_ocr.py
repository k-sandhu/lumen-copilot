"""Selective tenant-approved OCR cache and accounting (#695)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049_ocr"
down_revision = "0047_ingestion_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ingestion_stage_name", "ingestion_stage_outputs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_stage_name",
        "ingestion_stage_outputs",
        "stage IN ('detect','extract','ocr','normalize','classify','chunk','embed','index')",
    )
    op.add_column(
        "chunks", sa.Column("machine_read", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "ocr_tenant_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        *[
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
            for name in (
                "page_limit",
                "pages_used",
                "budget_microusd",
                "cost_used_microusd",
                "active_calls",
            )
        ],
        sa.Column("ceiling_microusd", sa.BigInteger(), nullable=False, server_default="10000"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", name="uq_ocr_policy_tenant"),
        sa.CheckConstraint(
            "page_limit >= 0 AND pages_used >= 0 AND budget_microusd >= 0 AND cost_used_microusd >= 0 AND active_calls >= 0 AND concurrency >= 1 AND concurrency <= 16 AND ceiling_microusd > 0",
            name="ck_ocr_policy_budgets",
        ),
        sa.CheckConstraint(
            "NOT enabled OR (approved_by IS NOT NULL AND page_limit > 0 AND budget_microusd > 0)",
            name="ck_ocr_policy_approval",
        ),
    )
    op.create_table(
        "ocr_page_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("engine_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "content_sha256", "engine_id", name="uq_ocr_page_identity"
        ),
        sa.CheckConstraint("state IN ('pending','complete','unknown')", name="ck_ocr_page_state"),
        sa.CheckConstraint("reserved_microusd > 0", name="ck_ocr_page_reservation"),
    )
    for table in ("ocr_tenant_policies", "ocr_page_cache"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY rls_{table} ON {table}
            USING (current_setting('app.tenant_id',true) = 'bypass' OR tenant_id = current_setting('app.tenant_id',true)::uuid)
            WITH CHECK (current_setting('app.tenant_id',true) = 'bypass' OR tenant_id = current_setting('app.tenant_id',true)::uuid)""")


def downgrade() -> None:
    op.drop_table("ocr_page_cache")
    op.drop_table("ocr_tenant_policies")
    op.drop_column("chunks", "machine_read")
    op.execute("DELETE FROM ingestion_stage_outputs WHERE stage = 'ocr'")
    op.execute("UPDATE documents SET ingestion_stage = NULL WHERE ingestion_stage = 'ocr'")
    op.drop_constraint("ck_ingestion_stage_name", "ingestion_stage_outputs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_stage_name",
        "ingestion_stage_outputs",
        "stage IN ('detect','extract','normalize','classify','chunk','embed','index')",
    )
