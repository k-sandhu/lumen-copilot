"""Content-free, tenant/document-bound comparison diagnostics."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_ingestion_shadow"
down_revision = "0047_ingestion_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "native_evidence_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_table(
        "ingestion_shadow",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("source_format", sa.String(16), nullable=False),
        sa.Column("comparison_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_ingestion_shadow_document_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "fingerprint", name="uq_ingestion_shadow_sample"
        ),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_ingestion_shadow_fingerprint"),
    )
    op.create_index("ix_ingestion_shadow_tenant_id", "ingestion_shadow", ["tenant_id"])
    op.execute("ALTER TABLE ingestion_shadow ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ingestion_shadow FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY ingestion_shadow_tenant ON ingestion_shadow
        USING (current_setting('app.tenant_id', true) = 'bypass' OR tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (current_setting('app.tenant_id', true) = 'bypass' OR tenant_id = current_setting('app.tenant_id', true)::uuid)""")


def downgrade() -> None:
    op.drop_table("ingestion_shadow")
    op.drop_column("documents", "native_evidence_locked")
