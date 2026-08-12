"""direct multipart sessions, media transcript provenance, timestamp citations (#571)

Revision ID: 0044_direct_media_uploads
Revises: 0043_code_run_resolved_packages
Create Date: 2026-08-11 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_direct_media_uploads"
down_revision: str | None = "0043_code_run_resolved_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_RLS_TABLES = (
    "document_uploads",
    "transcript_speakers",
    "transcript_segments",
    "transcription_checkpoints",
)
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) = 'bypass' OR "
    "tenant_id = current_setting('app.tenant_id', true)::uuid"
)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    # 5 GiB media objects exceed PostgreSQL INTEGER; widen the canonical byte count.
    op.alter_column(
        "documents", "size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger(), nullable=False
    )
    op.add_column(
        "documents",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="document"),
    )
    op.add_column("documents", sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.add_column(
        "documents", sa.Column("transcript_language", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("transcription_model", sa.String(length=255), nullable=True)
    )
    op.add_column("documents", sa.Column("ingestion_run_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_check_constraint(
        "ck_documents_kind", "documents", "kind in ('document', 'audio', 'video')"
    )
    op.create_check_constraint(
        "ck_documents_duration_nonneg",
        "documents",
        "duration_ms IS NULL OR duration_ms >= 0",
    )

    op.create_table(
        "document_uploads",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Reserved before the Document exists; intentionally not an FK.
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("provider_upload_id", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("part_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("last_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("document_id", name="uq_document_uploads_document_id"),
        sa.CheckConstraint("size_bytes > 0", name="ck_document_uploads_size_positive"),
        sa.CheckConstraint("part_size_bytes >= 5242880", name="ck_document_uploads_part_size"),
        sa.CheckConstraint(
            "part_count >= 1 AND part_count <= 10000", name="ck_document_uploads_part_count"
        ),
        sa.CheckConstraint(
            "state in ('initiated', 'completing', 'completed', 'aborted', 'expired', 'failed')",
            name="ck_document_uploads_state",
        ),
    )
    op.create_index("ix_document_uploads_tenant_id", "document_uploads", ["tenant_id"])
    op.create_index(
        "ix_document_uploads_tenant_owner", "document_uploads", ["tenant_id", "owner_id"]
    )
    op.create_index("ix_document_uploads_expires_at", "document_uploads", ["expires_at"])

    op.create_table(
        "transcript_speakers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("speaker_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("name_status", sa.String(length=20), nullable=False),
        sa.Column("name_confidence", sa.Float(), nullable=True),
        sa.Column("name_method", sa.String(length=40), nullable=True),
        sa.Column("evidence_segment_ids", _JSON, nullable=False),
        sa.UniqueConstraint("document_id", "speaker_id", name="uq_transcript_speaker"),
        sa.CheckConstraint(
            "name_status in ('unknown', 'inferred')", name="ck_transcript_speaker_name_status"
        ),
        sa.CheckConstraint(
            "name_confidence IS NULL OR (name_confidence >= 0 AND name_confidence <= 1)",
            name="ck_transcript_speaker_confidence",
        ),
    )
    op.create_index("ix_transcript_speakers_tenant_id", "transcript_speakers", ["tenant_id"])
    op.create_index("ix_transcript_speakers_document_id", "transcript_speakers", ["document_id"])

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("speaker_id", sa.String(length=64), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_transcript_segment_ordinal"),
        sa.UniqueConstraint("id", "document_id", name="uq_transcript_segments_id_document"),
        sa.CheckConstraint("ordinal >= 0", name="ck_transcript_segment_ordinal"),
        sa.CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms", name="ck_transcript_segment_time_span"
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name="ck_transcript_segment_char_span"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_transcript_segment_confidence",
        ),
    )
    op.create_index("ix_transcript_segments_tenant_id", "transcript_segments", ["tenant_id"])
    op.create_index("ix_transcript_segments_document_id", "transcript_segments", ["document_id"])

    op.create_table(
        "transcription_checkpoints",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("words", _JSON, nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "document_id", "chunk_index", "model", name="uq_transcription_checkpoint"
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_transcription_checkpoint_index"),
        sa.CheckConstraint(
            "start_ms >= 0 AND end_ms > start_ms", name="ck_transcription_checkpoint_time_span"
        ),
    )
    op.create_index(
        "ix_transcription_checkpoints_tenant_id", "transcription_checkpoints", ["tenant_id"]
    )
    op.create_index(
        "ix_transcription_checkpoints_document_id",
        "transcription_checkpoints",
        ["document_id"],
    )

    for table in ("chunks", "citations"):
        op.add_column(table, sa.Column("time_start_ms", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("time_end_ms", sa.BigInteger(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "transcript_segment_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("transcript_segments.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.add_column(table, sa.Column("speaker_id", sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column("speaker_name", sa.String(length=255), nullable=True))
        op.create_index(f"ix_{table}_transcript_segment_id", table, ["transcript_segment_id"])
        op.create_check_constraint(
            f"ck_{table}_time_span",
            table,
            "(time_start_ms IS NULL AND time_end_ms IS NULL) OR "
            "(time_start_ms >= 0 AND time_end_ms > time_start_ms)",
        )

    # A segment id is valid provenance only for its own document. Citations are
    # tied to the segment (or deliberate null) of their exact source chunk, so
    # alternate writers cannot forge cross-document timestamp evidence.
    op.create_unique_constraint(
        "uq_chunks_id_transcript_segment",
        "chunks",
        ["id", "transcript_segment_id"],
    )
    op.create_foreign_key(
        "fk_chunks_transcript_segment_document",
        "chunks",
        "transcript_segments",
        ["transcript_segment_id", "document_id"],
        ["id", "document_id"],
    )
    op.create_foreign_key(
        "fk_citations_chunk_transcript_segment",
        "citations",
        "chunks",
        ["chunk_id", "transcript_segment_id"],
        ["id", "transcript_segment_id"],
    )

    if op.get_context().dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_{table} ON {table} "
                f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
            )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_constraint("fk_citations_chunk_transcript_segment", "citations", type_="foreignkey")
    op.drop_constraint("fk_chunks_transcript_segment_document", "chunks", type_="foreignkey")
    op.drop_constraint("uq_chunks_id_transcript_segment", "chunks", type_="unique")

    for table in ("citations", "chunks"):
        op.drop_constraint(f"ck_{table}_time_span", table, type_="check")
        op.drop_index(f"ix_{table}_transcript_segment_id", table_name=table)
        op.drop_column(table, "speaker_name")
        op.drop_column(table, "speaker_id")
        op.drop_column(table, "transcript_segment_id")
        op.drop_column(table, "time_end_ms")
        op.drop_column(table, "time_start_ms")

    op.drop_table("transcription_checkpoints")
    op.drop_table("transcript_speakers")
    op.drop_table("transcript_segments")
    op.drop_table("document_uploads")

    op.drop_constraint("ck_documents_duration_nonneg", "documents", type_="check")
    op.drop_constraint("ck_documents_kind", "documents", type_="check")
    op.drop_column("documents", "ingestion_run_id")
    op.drop_column("documents", "transcription_model")
    op.drop_column("documents", "transcript_language")
    op.drop_column("documents", "duration_ms")
    op.drop_column("documents", "kind")
    op.alter_column(
        "documents", "size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer(), nullable=False
    )
