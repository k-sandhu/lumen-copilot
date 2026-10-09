"""Interleaved metadata writers preserve exact extraction and unrelated keys (#628)."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.ingestion import SourceLocation
from tests._db_helpers import copy_sqlite_schema


@pytest.mark.parametrize("extraction_first", [False, True], ids=["diagnostics", "extraction"])
async def test_interleaved_writers_preserve_current_source_map_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extraction_first: bool
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'interleaved.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(copy_sqlite_schema)
        async with factory.begin() as session:
            tenant = await TenantRepository(session).create(name="Interleaved")
            user = await UserRepository(session, tenant.id).create(
                email="writer@example.test", password_hash="h", roles=[Role.MEMBER]
            )
            collection = await CollectionRepository(session, tenant.id).create(
                owner_id=user.id, name="sources"
            )
            document = await DocumentRepository(session, tenant.id).create(
                owner_id=user.id,
                collection_id=collection.id,
                filename="source.pdf",
                mime_type="application/pdf",
                size_bytes=3,
                storage_key="synthetic",
                acl_enforced=False,
            )
            await DocumentRepository(session, tenant.id).set_extraction(
                document.id, text="old", locations=[SourceLocation("page", "old", 1, 0, 3)]
            )
            await DocumentRepository(session, tenant.id).update_ingestion_metadata(
                document.id, {"unchanged": {"key": "value"}}
            )

        paused = asyncio.Event()
        committed = asyncio.Event()
        new_location = SourceLocation("page", "activated", 1, 0, 7)

        async def first_writer() -> None:
            async with factory.begin() as session:
                # Cache the ORM row too: current-state writes must not trust it.
                cached = (await session.execute(select(models.Document))).scalar_one()
                execute = session.execute
                intercepted = False

                async def interleave(statement, *args, **kwargs):
                    nonlocal intercepted
                    if intercepted:
                        return await execute(statement, *args, **kwargs)
                    intercepted = True
                    if statement.is_select:
                        result = await execute(statement, *args, **kwargs)
                        paused.set()
                        await committed.wait()
                        return result
                    # An atomic UPDATE is delayed before dispatch, while the old
                    # read/replace writer is delayed after reading its snapshot.
                    paused.set()
                    await committed.wait()
                    return await execute(statement, *args, **kwargs)

                monkeypatch.setattr(session, "execute", interleave)
                repository = DocumentRepository(session, tenant.id)
                if extraction_first:
                    await repository.set_extraction(
                        document.id, text="newtext", locations=[new_location]
                    )
                else:
                    await repository.update_ingestion_metadata(
                        document.id, {"extraction_diagnostics": {"character_count": 7}}
                    )
                assert cached.id == document.id

        async def second_writer() -> None:
            await paused.wait()
            try:
                async with factory.begin() as session:
                    repository = DocumentRepository(session, tenant.id)
                    if not extraction_first:
                        await repository.set_extraction(
                            document.id, text="newtext", locations=[new_location]
                        )
                    await repository.update_ingestion_metadata(
                        document.id,
                        {
                            "other_attempt": "preserved",
                            "extraction_diagnostics": {"character_count": 7},
                        },
                    )
            finally:
                committed.set()

        await asyncio.gather(first_writer(), second_writer())
        async with factory.begin() as session:
            repository = DocumentRepository(session, tenant.id)
            current = await repository.get(document.id)
            assert current is not None
            assert current.source_text == "newtext"
            assert current.source_locations == (new_location,)
            assert current.ingestion_metadata == {
                "source_locations": [new_location.to_dict()],
                "unchanged": {"key": "value"},
                "other_attempt": "preserved",
                "extraction_diagnostics": {"character_count": 7},
            }
            foreign = DocumentRepository(session, uuid4())
            assert await foreign.update_ingestion_metadata(document.id, {"leak": True}) is None
            assert await foreign.set_extraction(document.id, text="leak", locations=[]) is None
            # Null values remain explicit, rather than deleting their keys.
            cleared = await repository.update_ingestion_metadata(
                document.id, {"extraction_diagnostics": None}
            )
            assert cleared is not None
            assert cleared.ingestion_metadata["extraction_diagnostics"] is None
            assert cleared.source_locations == (new_location,)
    finally:
        await engine.dispose()
