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

**Backfill.** A pre-existing row WITH an address came from a client — that much is
certain. A row WITHOUT one is *not* recoverable, and is backfilled ``unknown`` rather
than ``system``:

    On Postgres a background task's ``"system"`` sentinel could never persist at all —
    ``INET`` rejected it and took the whole transaction with it. So a surviving
    NULL-``source_ip`` row is NOT a system actor. It is a caller that passed a bare
    ``None``, which before this change only the ``/auth`` routes did, when
    ``request.client`` was unset. That is a real client we could not see — precisely
    what ``unknown`` means. Backfilling those to ``system`` would assert that the
    platform performed a user's login, which is both false and exactly the
    client/platform confusion ``source_origin`` exists to prevent.

The ``server_default`` exists SOLELY to backfill and is **dropped again in this same
migration** (the 0040 pattern), so no insert path can ever silently default an
event's origin.

**The backfill is DML on a table the app role may not UPDATE.** Migration 0002 revokes
``UPDATE``/``DELETE`` on ``audit_events`` (append-only, spec 0004 §2.4) and 0007 puts
``FORCE ROW LEVEL SECURITY`` on it. A superuser bypasses both — which is why this
migration passed a first live run without anyone noticing — but on the least-privilege
role the spec actually describes, a bare ``UPDATE`` aborts with *permission denied*.
Worse, with ``UPDATE`` granted but RLS still forced, a migration connection sets no
``app.tenant_id`` GUC, so the policy predicate is NULL for every row, the UPDATE
matches nothing, and the CHECK constraint then fails against rows left at the default.
The backfill therefore grants itself ``UPDATE`` and lifts ``FORCE`` for the duration,
then restores both — all Postgres-only, since SQLite has neither.

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

    # A row with no address is a client we could not see, not the platform acting —
    # see the module docstring. Postgres needs the append-only UPDATE grant lent back
    # and FORCE RLS lifted, or this either refuses outright or silently matches zero
    # rows and fails the CHECK below.
    is_postgres = op.get_bind().dialect.name == "postgresql"
    if is_postgres:
        op.execute("GRANT UPDATE ON TABLE audit_events TO CURRENT_USER")
        # NOT the policy's `app.tenant_id = 'bypass'` sentinel. That works for the
        # app's row-scoped queries but NOT for a bulk UPDATE: the policy is
        # `current_setting(...) = 'bypass' OR tenant_id = current_setting(...)::uuid`,
        # and over a full scan the planner evaluates the cast rather than
        # short-circuiting the OR, so it dies on `invalid input syntax for type uuid:
        # "bypass"`. Verified against the live database as a non-superuser owner.
        # `NO FORCE` is the mechanism that actually applies here: FORCE is precisely
        # what subjects the OWNER to the policy (migration 0007), and the app role owns
        # this table, so lifting it for the backfill restores the ordinary owner
        # exemption and nothing more.
        op.execute("ALTER TABLE audit_events NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE audit_events SET source_origin = 'unknown' WHERE source_ip IS NULL")
    if is_postgres:
        # No `try/finally` around this pair. Alembic runs on transactional DDL, so a
        # failed backfill rolls the GRANT and the NO FORCE back with everything else —
        # and a `finally` would fire INSIDE the aborted transaction, where Postgres
        # rejects every statement, replacing the real error with
        # `InFailedSQLTransactionError` and hiding the cause. (Learned the hard way:
        # the first version of this did exactly that and cost a debugging round.)
        op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
        op.execute("REVOKE UPDATE ON TABLE audit_events FROM CURRENT_USER")

    op.alter_column("audit_events", "source_origin", server_default=None)
    op.create_check_constraint(
        "ck_audit_events_origin_ip_agree",
        "audit_events",
        _ORIGIN_IP_AGREE,
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_events_origin_ip_agree", "audit_events", type_="check")
    op.drop_column("audit_events", "source_origin")
