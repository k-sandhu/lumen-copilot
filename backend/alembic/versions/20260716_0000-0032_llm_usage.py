"""llm_usage — per-answer token & cache accounting (#409, ADR-0016 §2.6)

The persistence half of the usage substrate: one row per produced answer (chat
and headless runs alike — both ride the shared runtime), summing token usage
across every completion turn of the answer's loop, including the provider cache
accounting (``cached_prompt_tokens`` served from the provider's prompt cache,
``cache_write_tokens`` written into it). This is the substrate the cache-hit
KPI, AgentOps analytics (#300), credit budgets, and the ADR-0018 sub-agent
budgets read — the record, not the dashboard.

A trace/analytics table like ``tool_invocations`` (ordinary tenant-scoped
access, no UPDATE/DELETE revoke; write-once, so ``created_at`` only). Links are
nullable with ``ON DELETE SET NULL`` so deleting a session/message keeps the
accounting. ``message_id`` is an ORDINARY immediate FK: unlike
``tool_invocations`` (written mid-loop before the message insert, hence its
deferred FK), the usage row is recorded strictly AFTER ``_persist`` flushes the
message in the same transaction, so there is no forward reference to defer.
``run_id`` is reserved for headless-run / sub-agent grouping (no FK yet,
mirroring ``tool_invocations.run_id``).

Indexes: ``(tenant_id, session_id)`` (per-chat spend), ``(tenant_id,
created_at)`` (time-window analytics), and a PARTIAL unique
``(tenant_id, message_id) WHERE message_id IS NOT NULL`` making "one row per
answer" structural — each tenant-leading (INV-1), plus the mixin's plain
``tenant_id`` index.

RLS: tenant-scoped, so this migration ``ENABLE`` + ``FORCE`` row-level security
with the same fail-closed ``app.tenant_id`` GUC policy the ``0007`` backstop
uses for every other tenant-scoped table (spec 0004 §2.1) — additive.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS,
and drops the table. Plain DDL so it renders offline; the live apply runs
against a disposable throwaway database.

Revision ID: 0032_llm_usage
Revises: 0031_toolinv_ordinal_msg_idx
Create Date: 2026-07-16 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032_llm_usage"
down_revision: str | None = "0031_toolinv_ordinal_msg_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
        "llm_usage",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The chat the answer belongs to — nullable, SET NULL so deleting a
        # session keeps the accounting (spend outlives its subjects).
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The assistant message the answer persisted as. ORDINARY immediate FK
        # (#419 review): the usage row is recorded strictly AFTER _persist flushes
        # the message row in the same transaction, so there is no forward
        # reference and no deferral is needed (unlike tool_invocations, written
        # DURING the loop before the message insert).
        sa.Column(
            "message_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Reserved for headless-run / sub-agent grouping (ADR-0018); no FK yet.
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        # The REQUESTED model id — 255 to MATCH messages.model (#419 review): the
        # value is copied from the same id, so a narrower width could fail the
        # usage insert (and the whole answer) for a model id the message accepts.
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0 "
            "AND cached_prompt_tokens >= 0 AND cache_write_tokens >= 0",
            name="ck_llm_usage_tokens_nonneg",
        ),
    )
    # Tenant-leading indexes (INV-1) — the mixin's plain tenant index plus the
    # two read paths: per-chat spend and time-window analytics (#300).
    op.create_index("ix_llm_usage_tenant_id", "llm_usage", ["tenant_id"])
    op.create_index("ix_llm_usage_tenant_session", "llm_usage", ["tenant_id", "session_id"])
    op.create_index("ix_llm_usage_tenant_created", "llm_usage", ["tenant_id", "created_at"])
    # "One row per answer" made structural (#419 review): a second record for the
    # same assistant message is an IntegrityError, not silent double-counting.
    # PARTIAL (message_id IS NOT NULL) so historical rows whose message was
    # deleted (SET NULL) — and future message-less rows (headless/sub-agent,
    # keyed by run_id, ADR-0016 §2.6) — stay unconstrained. Postgres and SQLite
    # both honour the partial predicate; the ORM carries both dialect where-args.
    op.create_index(
        "uq_llm_usage_tenant_message",
        "llm_usage",
        ["tenant_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
        sqlite_where=sa.text("message_id IS NOT NULL"),
    )

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE llm_usage ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE llm_usage FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_llm_usage ON llm_usage "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_llm_usage ON llm_usage")
        op.execute("ALTER TABLE llm_usage NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE llm_usage DISABLE ROW LEVEL SECURITY")
    op.drop_index("uq_llm_usage_tenant_message", table_name="llm_usage")
    op.drop_index("ix_llm_usage_tenant_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_tenant_session", table_name="llm_usage")
    op.drop_index("ix_llm_usage_tenant_id", table_name="llm_usage")
    op.drop_table("llm_usage")
