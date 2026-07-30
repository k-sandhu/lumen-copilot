"""audit_source_origin — a typed origin so ``source_ip`` can be honest (#546)

``audit_events.source_ip`` is ``INET`` on Postgres, but the envelope has always
required a non-empty value (spec 0004 §2.4) and a background task has no client
address. Callers therefore passed the sentinel ``"system"``, Postgres rejected it,
and because the audit write deliberately rides the CALLER's transaction the
rejection rolled back the action being recorded — which is why the rolling session
summariser had never once persisted on Postgres.

This migration makes the contract stateable rather than exception-ridden:

* ``source_origin`` — **NOT NULL**. ``client`` (a real peer, address recorded) ·
  ``system`` (background/scheduled work, no client exists) · ``unknown`` (a request
  whose peer address could not be determined — an AF_UNIX peer makes
  ``request.client`` ``None``). ``unknown`` is deliberately distinct from
  ``system``: "a person did this and we could not see from where" is a different
  operational fact from "the platform did this".
* ``source_ip`` — nullable BY CONTRACT, and non-null **iff** ``source_origin =
  'client'``. Enforced by a CHECK constraint, so the invariant lives in the
  database rather than only in the service that writes it.

**Backfill.** Every pre-existing row predates the sentinel fix, so its origin is
recoverable exactly: a row with an address came from a client, and a row without one
came from a system actor (before this change no other combination could persist —
a sentinel would have aborted the transaction). The ``server_default`` exists SOLELY
to backfill and is **dropped again in this same migration** (the 0040 pattern), so
no insert path can ever silently default an event's origin.

Reversible: ``downgrade`` drops the constraint and the column. The append-only
grant on ``audit_events`` permits ``ALTER TABLE``; no row is rewritten on the way
back out.

Revision ID: 0041_audit_source_origin
Revises: 0040_gdrive_acl
Create Date: 2026-07-30 00:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_audit_source_origin"
down_revision = "0040_gdrive_acl"
branch_labels = None
depends_on = None

#: Non-null iff the event came from a client. The database, not just the writer,
#: refuses "a client action with no address" and "a system action with one".
_ORIGIN_IP_AGREE = (
    "(source_origin = 'client' AND source_ip IS NOT NULL) "
    "OR (source_origin <> 'client' AND source_ip IS NULL)"
)


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column(
            "source_origin",
            sa.String(length=16),
            nullable=False,
            # Backfill only — dropped below so no write path inherits it.
            server_default="client",
        ),
    )
    # A row that never recorded an address was a system actor: before this change a
    # sentinel could not persist at all, so NULL is unambiguous.
    op.execute("UPDATE audit_events SET source_origin = 'system' WHERE source_ip IS NULL")
    op.alter_column("audit_events", "source_origin", server_default=None)
    op.create_check_constraint(
        "ck_audit_events_origin_ip_agree",
        "audit_events",
        _ORIGIN_IP_AGREE,
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_events_origin_ip_agree", "audit_events", type_="check")
    op.drop_column("audit_events", "source_origin")
