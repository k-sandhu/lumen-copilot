"""tool_invocations.message_id FK → DEFERRABLE INITIALLY DEFERRED (chat answer fix)

The chat runtime (``services/chat_runtime._answer``) records a ``tool_invocations``
row **during** the tool loop, keyed to the pre-minted assistant ``message_id`` —
but the ``messages`` row itself is only INSERTed by ``_persist`` at the *end* of
the SAME transaction (a single ``commit`` at the boundary). With the original
IMMEDIATE foreign key the tool-invocation INSERT was validated at flush — before
the message row existed — so every tool-using answer (i.e. every grounded answer,
since retrieval is a governed tool) aborted with ``IntegrityError`` and surfaced
to the user as a chat "internal server error". The offline SQLite test engine
does not enforce foreign keys, so CI stayed green while real Postgres broke — the
same class of gap as the autoflush regression.

Making the FK ``DEFERRABLE INITIALLY DEFERRED`` validates it at COMMIT instead,
by which point ``_persist`` has inserted the message: the reference is a
legitimate intra-transaction forward reference. ``ON DELETE SET NULL`` is
retained (a deleted message keeps the trace row, message link nulled).

Data-preserving (only the constraint's check *timing* changes). Reversible:
``downgrade`` restores the immediate FK. Offline DDL render asserts the shape.

Revision ID: 0022_toolinv_msg_fk_deferrable
Revises: 0021_tenant_tool_policy
Create Date: 2026-07-03 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_toolinv_msg_fk_deferrable"
down_revision: str | None = "0021_tenant_tool_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tool_invocations"
_FK = "tool_invocations_message_id_fkey"


def upgrade() -> None:
    # Recreate the message_id FK as deferrable so its check moves to commit time.
    op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _FK,
        _TABLE,
        "messages",
        ["message_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    # Restore the original IMMEDIATE FK (same columns + ON DELETE SET NULL).
    op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _FK,
        _TABLE,
        "messages",
        ["message_id"],
        ["id"],
        ondelete="SET NULL",
    )
