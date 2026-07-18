"""session_summaries — rolling session summary + evidence carry-forward (#416)

ADR-0016 §3.2: one row per chat session holding (a) the rolling SUMMARY of
turns older than the verbatim window — conversational content only, never
source-document text — updated asynchronously post-answer under a tenant-bound
Celery scope, and (b) the last answer's cited-evidence digest as **IDs only**
(document + chunk uuids; the next answer rehydrates them through ``retrieval/``
under the requester's CURRENT permissions — INV-2). ``covers_through_message_id``
marks how far the summary reaches (assembly sends only newer turns verbatim);
``version`` increments per summary write (cache-aligned prefix invalidation).

Tenant-scoped like every other table: ``tenant_id`` FK + the fail-closed 0007
RLS backstop. One row per session (unique) — the summary is a rolling
replacement, not a log.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy + table.

Revision ID: 0037_session_summaries
Revises: 0036_llm_usage_answer
Create Date: 2026-07-17 04:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0037_session_summaries"
down_revision: str | None = "0036_llm_usage_answer"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

# The GUC + bypass sentinel the RLS policy reads — kept in lockstep with the
# ``0007`` backstop and ``app.db.tenant_context`` (TENANT_GUC / BYPASS_SENTINEL).
_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        "session_summaries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # One row per session; the summary dies with its session.
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The rolling summary text (conversational content only). NULL until the
        # first async summarize completes — evidence may exist earlier.
        sa.Column("summary", sa.Text(), nullable=True),
        # The coverage CURSOR (#446 review, finding 5): a durable TOTAL ORDER —
        # (created_at, id) of the newest covered message — usable for SQL-side
        # "newer than" filtering and forward-only compare-and-swap even after
        # the boundary row is pruned. No FK on the id for that reason.
        sa.Column("covers_through_message_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "covers_through_created_at", sa.DateTime(timezone=True), nullable=True
        ),
        # The last answer's cited-evidence digest — IDs ONLY (a JSON list of
        # {"document_id": uuid, "chunk_id": uuid}); rehydrated under the
        # requester's CURRENT permissions at next-answer assembly (INV-2).
        sa.Column("evidence", _JSON, nullable=True),
        # Documents the summary MENTIONS by name (#446 review, finding 1):
        # {document_id: name} captured at write time from the covered turns'
        # citations, so the read path can redact names whose documents the
        # requester can no longer retrieve (revocation-safe memory).
        sa.Column("mentioned_documents", _JSON, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "session_id", name="uq_session_summaries_session"),
    )
    op.create_index(
        "ix_session_summaries_tenant_session",
        "session_summaries",
        ["tenant_id", "session_id"],
    )

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE session_summaries ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE session_summaries FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_session_summaries ON session_summaries "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_session_summaries ON session_summaries")
        op.execute("ALTER TABLE session_summaries NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE session_summaries DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_session_summaries_tenant_session", table_name="session_summaries")
    op.drop_table("session_summaries")
