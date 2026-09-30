"""Distinguish bounded cookie admissions from legacy rotation history (#580).

Existing rows default to false: before the slot protocol, every rotation wrote
a new row for the same fixed cookie name. Preserve that history and the live
credential. New login admissions explicitly set true and retain their charge
through revocation until absolute expiry; rotation never changes provenance.

Revision ID: 0044_refresh_cookie_admission
Revises: 0043_code_run_resolved_packages
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_refresh_cookie_admission"
down_revision = "0043_code_run_resolved_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("cookie_admitted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "cookie_admitted")
