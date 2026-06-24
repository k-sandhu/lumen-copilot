"""Seed a dev user so login is testable locally (CC-3 / spec 0004 §2.3).

A small, idempotent utility (not a product surface — self-service registration
is OUT of scope, issue #19). Run against a live, migrated database::

    uv run python -m app.auth.seed --email dev@acme.test --password devpass \\
        --tenant Acme --role member --role admin

It creates the tenant if absent, then creates (or leaves) the user with an
Argon2id password hash and the given roles. All persistence goes through the
``db/`` repositories and hashing through ``auth/`` — no raw SQL, no second hasher.
Defaults seed ``dev@acme.test`` / ``devpass`` in tenant ``Acme`` with the
``member`` and ``admin`` roles.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select

from app.auth import hash_password
from app.db import models
from app.db.repositories import TenantRepository, UserRepository
from app.db.session import dispose_engine, session_scope
from app.db.tenant_context import bind_bypass
from app.domain.entities import Role, User


async def seed_user(
    *,
    email: str,
    password: str,
    tenant_name: str,
    roles: list[Role],
) -> User:
    """Idempotently ensure a tenant + user exist; return the user.

    If the email already exists (within any tenant) the existing user is
    returned unchanged — re-running the seed is safe.
    """
    async with session_scope() as session:
        # The seed is a pre-identity/system path: it creates the first tenant and
        # a user with no tenant context yet, so it opts into the RLS bypass
        # sentinel (#17) for this transaction. Without it, the user INSERT and the
        # tenant-name lookup would be rejected/empty under row-level security. A
        # no-op off Postgres (the offline tests never run the seed against PG).
        await bind_bypass(session)
        # Resolve or create the tenant by name (dev convenience; names are not
        # unique in the schema, so we reuse the first match if present).
        tenant_row = (
            await session.execute(select(models.Tenant).where(models.Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if tenant_row is None:
            tenant = await TenantRepository(session).create(name=tenant_name)
            tenant_id: UUID = tenant.id
        else:
            tenant_id = tenant_row.id

        users = UserRepository(session, tenant_id)
        existing = await users.get_by_email(email)
        if existing is not None:
            return existing

        return await users.create(
            email=email,
            password_hash=hash_password(password),
            roles=roles,
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a dev user (CC-3).")
    parser.add_argument("--email", default="dev@acme.test")
    parser.add_argument("--password", default="devpass")
    parser.add_argument("--tenant", default="Acme")
    parser.add_argument(
        "--role",
        action="append",
        choices=[r.value for r in Role],
        dest="roles",
        help="May be passed multiple times; defaults to member+admin.",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    roles = [Role(r) for r in (args.roles or [Role.MEMBER.value, Role.ADMIN.value])]
    user = await seed_user(
        email=args.email,
        password=args.password,
        tenant_name=args.tenant,
        roles=roles,
    )
    try:
        print(  # noqa: T201 — CLI feedback is the point
            f"seeded user {user.email} (id={user.id}) in tenant {user.tenant_id} "
            f"with roles {[r.value for r in user.roles]}"
        )
    finally:
        await dispose_engine()


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    asyncio.run(_main())
