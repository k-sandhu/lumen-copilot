"""Retain immutable answer provenance independently of citations (#569, R1-001).

Legacy NULLs deliberately stay unknown: surviving citations cannot prove that
other citations have not already cascaded away. Recall withholds those answers.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_message_source_provenance"
down_revision = "0045_embedding_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("source_document_ids", postgresql.JSONB(), nullable=True))
    op.execute("""
        CREATE FUNCTION preserve_message_source_provenance() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.source_document_ids IS DISTINCT FROM OLD.source_document_ids THEN
                RAISE EXCEPTION 'message source provenance is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER messages_source_provenance_immutable
        BEFORE UPDATE OF source_document_ids ON messages
        FOR EACH ROW EXECUTE FUNCTION preserve_message_source_provenance()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER messages_source_provenance_immutable ON messages")
    op.execute("DROP FUNCTION preserve_message_source_provenance()")
    op.drop_column("messages", "source_document_ids")
