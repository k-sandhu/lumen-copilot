"""Tenant-scoped operational ingestion checkpoints (#669)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_ingestion_stages"
down_revision = "0046_message_source_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_documents_tenant_id_id", "documents", ["tenant_id", "id"])
    op.add_column("documents", sa.Column("ingestion_stage", sa.String(16), nullable=True))
    op.create_table(
        "ingestion_stage_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_ingestion_stage_document_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "stage", name="uq_ingestion_stage_document"
        ),
        sa.CheckConstraint(
            "stage IN ('detect','extract','normalize','classify','chunk','embed','index')",
            name="ck_ingestion_stage_name",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64 AND length(output_sha256) = 64",
            name="ck_ingestion_stage_hashes",
        ),
    )
    op.create_index(
        "ix_ingestion_stage_outputs_tenant_id", "ingestion_stage_outputs", ["tenant_id"]
    )
    op.execute("ALTER TABLE ingestion_stage_outputs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ingestion_stage_outputs FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY ingestion_stage_outputs_tenant ON ingestion_stage_outputs
        USING (current_setting('app.tenant_id', true) = 'bypass'
            OR tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (current_setting('app.tenant_id', true) = 'bypass'
            OR tenant_id = current_setting('app.tenant_id', true)::uuid)""")


def downgrade() -> None:
    op.drop_table("ingestion_stage_outputs")
    op.drop_column("documents", "ingestion_stage")
    op.drop_constraint("uq_documents_tenant_id_id", "documents", type_="unique")
