"""Tenant-bound administrator report, inventory and original-byte shadow replay CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from app.auth import InvalidTokenError, verify_access_token
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.repositories import UserRepository
from app.db.session import dispose_engine, get_durable_audit_transactions, tenant_session_scope
from app.services.audit import PermissionDeniedContext, PermissionDeniedRecorder
from app.services.ingestion_admin import IngestionAdminService, summarize
from app.storage import ObjectStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Administrator ingestion diagnostics; no citation replacement."
    )
    parser.add_argument(
        "command", choices=["report", "preview", "replay-originals", "execute-generation"]
    )
    parser.add_argument(
        "--token-file", type=Path, required=True, help="Local access JWT file; never printed."
    )
    scopes = parser.add_mutually_exclusive_group()
    scopes.add_argument("--document", type=UUID)
    scopes.add_argument("--collection", type=UUID)
    parser.add_argument("--cursor", type=UUID)
    parser.add_argument(
        "--pages", type=int, default=1, help="At most 100 records/documents per page."
    )
    parser.add_argument(
        "--diagnostics", action="store_true", help="Report authorized document IDs and counters."
    )
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 1000:
        parser.error("pages must be between 1 and 1000")
    if args.command == "report" and args.collection:
        parser.error("report supports document or tenant scope")
    return args


async def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    store = ObjectStore.from_settings(settings)
    try:
        if args.token_file.stat().st_size > 16384:
            raise InvalidTokenError()
        principal = verify_access_token(
            args.token_file.read_text(encoding="utf-8").strip(), settings
        )
        async with tenant_session_scope(principal.tenant_id) as session:
            user = await UserRepository(session, principal.tenant_id).get(principal.user_id)
            if user is None:
                raise InvalidTokenError()
            current = Principal(
                principal.user_id,
                principal.tenant_id,
                tuple(role for role in user.roles if role in principal.roles),
            )
            denials = PermissionDeniedContext(
                PermissionDeniedRecorder(
                    get_durable_audit_transactions(settings),
                    tenant_id=current.tenant_id,
                    request_session=session,
                ),
                principal=current,
                request_id="ingestion-admin-cli",
                source_ip="system",
            )
            service = IngestionAdminService(
                current, settings=settings, object_store=store, denials=denials
            )
            if args.command == "execute-generation":
                await service.execute_generation(document_id=args.document)
            cursor = args.cursor
            aggregate: dict[str, dict[str, int]] = {}
            selected = 0
            for _ in range(args.pages):
                if args.command == "report":
                    page = await service.report(document_id=args.document, cursor=cursor)
                    for fmt, values in summarize(page.rows).items():
                        bucket = aggregate.setdefault(fmt, {})
                        for key, count in values.items():
                            bucket[key] = bucket.get(key, 0) + count
                    if args.diagnostics:
                        for row in page.rows:
                            print(
                                json.dumps(
                                    {"document_id": str(row.document_id), **asdict(row.comparison)},
                                    sort_keys=True,
                                )
                            )  # noqa: T201
                    cursor = page.next_cursor
                else:
                    inventory = await service.preview(
                        document_id=args.document, collection_id=args.collection, cursor=cursor
                    )
                    for document_id, _fmt in inventory.documents:
                        selected += 1
                        if args.command == "replay-originals":
                            result = await service.replay(document_id)
                            print(
                                json.dumps(
                                    {"document_id": str(document_id), **asdict(result)},
                                    sort_keys=True,
                                )
                            )  # noqa: T201
                    cursor = inventory.next_cursor
                if cursor is None:
                    break
            print(
                json.dumps(
                    {
                        "formats": aggregate,
                        "selected": selected,
                        "next_cursor": str(cursor) if cursor else None,
                        "generation_activation": "requires_owner_policy",
                    },
                    sort_keys=True,
                )
            )  # noqa: T201
    except AppError as error:
        print(json.dumps({"code": error.code, "status": error.status}))  # noqa: T201
        raise SystemExit(2) from None
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
