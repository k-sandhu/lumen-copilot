"""Evidence-based provider validation columns + status domain (spec 0029, #585).

Adds ``validation_checks`` / ``last_error_code`` / ``validated_at`` /
``validation_grandfathered`` to ``llm_providers`` and widens the status CHECK
with ``reachable``. Existing ``ready`` rows are grandfathered: they stay
``ready`` (routable) with ``validation_grandfathered = true`` and honest
checks (auth/capabilities unverified) until their next validation, after
which the strict rule applies — existing setups do not silently disappear on
deploy. Existing ``error`` rows have their free-text ``last_error`` scrubbed
(it could carry raw vendor/network text) to the fixed safe message.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_provider_validation"
down_revision = "0050_assistant_bound"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")

_GRANDFATHER_CHECKS = (
    '[{"name": "config_valid", "status": "passed", "code": null}, '
    '{"name": "reachable", "status": "passed", "code": null}, '
    '{"name": "catalog_discovered", "status": "passed", "code": null}, '
    '{"name": "auth_verified", "status": "unverified", "code": null}, '
    '{"name": "capabilities_verified", "status": "unverified", "code": null}]'
)


def upgrade() -> None:
    op.add_column(
        "llm_providers",
        sa.Column("validation_checks", _JSON, nullable=False, server_default="[]"),
    )
    op.add_column("llm_providers", sa.Column("last_error_code", sa.String(50), nullable=True))
    op.add_column(
        "llm_providers",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("llm_providers", sa.Column("validation_result", _JSON, nullable=True))
    op.add_column(
        "llm_providers",
        sa.Column(
            "validation_grandfathered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.drop_constraint("ck_llm_providers_status", "llm_providers", type_="check")
    op.create_check_constraint(
        "ck_llm_providers_status",
        "llm_providers",
        "status in ('pending', 'reachable', 'ready', 'error')",
    )
    # Migration owners must see all tenants under FORCE RLS. Transactional DDL
    # restores FORCE on rollback too (same mechanism as migration 0042).
    op.execute("ALTER TABLE llm_providers NO FORCE ROW LEVEL SECURITY")
    # Grandfather currently-ready providers (see module docstring).
    op.execute(
        "UPDATE llm_providers SET validation_grandfathered = true, "
        f"validation_checks = '{_GRANDFATHER_CHECKS}', "
        "validated_at = last_discovery_at WHERE status = 'ready'"
    )
    # Scrub possibly-leaky free-text errors recorded before the safe boundary.
    op.execute(
        "UPDATE llm_providers SET last_error = 'Provider validation failed.', "
        "last_error_code = 'validation_failed' "
        "WHERE last_error IS NOT NULL"
    )
    # Scrub legacy key fragments from both read paths. Audit history is
    # append-only; historical events are not rewritten by this migration.
    op.execute("UPDATE llm_providers SET secret_hint = NULL")
    op.execute("ALTER TABLE secrets NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE secrets SET hint = '****' WHERE id IN "
        "(SELECT api_key_secret_ref FROM llm_providers WHERE api_key_secret_ref IS NOT NULL)"
    )
    op.execute("ALTER TABLE secrets FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_providers FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # ``reachable`` has no pre-0050 equivalent; fold it back to ``pending``
    # (catalog known, readiness unproven — the closest honest old state).
    op.execute("ALTER TABLE llm_providers NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE llm_providers SET status = 'pending' WHERE status = 'reachable'")
    op.execute("ALTER TABLE llm_providers FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_llm_providers_status", "llm_providers", type_="check")
    op.create_check_constraint(
        "ck_llm_providers_status",
        "llm_providers",
        "status in ('pending', 'ready', 'error')",
    )
    op.drop_column("llm_providers", "validation_grandfathered")
    op.drop_column("llm_providers", "validated_at")
    op.drop_column("llm_providers", "last_error_code")
    op.drop_column("llm_providers", "validation_checks")
    op.drop_column("llm_providers", "validation_result")
