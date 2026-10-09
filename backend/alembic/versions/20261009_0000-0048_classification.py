"""Tenant-bound classification work, controls and conservative spend ledger."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_classification"
down_revision = "0047_ingestion_stages"
branch_labels = None
depends_on = None


def _tenant() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(n, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        for n in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "classification_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant(),
        sa.Column("controls", postgresql.JSONB(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", name="uq_classification_policy_tenant"),
    )
    op.create_table(
        "document_classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("extraction_id", sa.String(64), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("override_path", sa.String(255), nullable=True),
        sa.Column("override_actor", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("override_reason", sa.String(1000), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "document_id", name="uq_classification_document"),
        sa.CheckConstraint("revision >= 0 AND retries >= 0", name="ck_classification_counters"),
    )
    op.create_table(
        "classification_spend",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant(),
        sa.Column("ceiling_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("attempt", postgresql.JSONB(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "ceiling_usd > 0 AND (cost_usd IS NULL OR cost_usd >= 0)",
            name="ck_classification_spend",
        ),
    )
    for table in ("classification_policies", "document_classifications", "classification_spend"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON {table}
            USING (current_setting('app.tenant_id', true) = 'bypass'
                OR tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (current_setting('app.tenant_id', true) = 'bypass'
                OR tenant_id = current_setting('app.tenant_id', true)::uuid)""")


def downgrade() -> None:
    for table in ("classification_spend", "document_classifications", "classification_policies"):
        op.drop_table(table)
