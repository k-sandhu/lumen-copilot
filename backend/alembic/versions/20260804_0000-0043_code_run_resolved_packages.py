"""code_runs.resolved_packages — what a run actually installed (#509)

``code_runs.requested_packages`` records what the MODEL asked for. ``pip install``
takes the whole resolved wheelhouse, so a run that requested ``requests`` and pulled in
``urllib3``, ``idna``, ``certifi`` and ``charset-normalizer`` audited as one package.
Two consequences, both bad for a trail that exists to answer "what ran":

* an operator could not tell whether a distribution later found to be malicious had
  ever been installed, and
* a **denied** distribution admitted transitively left no trace at all.

The runner is the only component that sees the resolution — it is what performs the
``pip download`` — so it now reports the manifest and this column persists it:
``[{"name": ..., "version": ..., "sha256": ...}]``, one entry per wheel, with the
digest of the artefact as fetched.

Nullable with no server default. An empty list would claim "this run installed
nothing", which is a real and different fact from "this run predates the column" —
every row written before this migration is the second, and saying so honestly is worth
a nullable column.

Revision ID: 0043_code_run_resolved_packages
Revises: 0042_audit_source_origin
Create Date: 2026-08-04 00:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_code_run_resolved_packages"
down_revision = "0042_audit_source_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_runs",
        sa.Column(
            "resolved_packages",
            # JSONB on Postgres so an operator can query "which runs installed X"
            # without parsing text; plain JSON keeps the SQLite test path working.
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("code_runs", "resolved_packages")
