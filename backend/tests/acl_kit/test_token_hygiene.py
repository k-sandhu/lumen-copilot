"""INV-6 + the no-token-material guarantee, for the managed-connector path.

ADR-0019's last negative-test row: *token material never in ``sources.config``,
logs, audit metadata, or any API response (grep + serialization tests)*, plus
the INV-6 bullet the kit owns — ``secret.accessed`` naming the **system** actor
when the sync worker resolves a connector credential.

Two kinds of proof, because either alone is weak:

* **runtime serialization** — a real sync reads a real vault-held refresh token,
  and the token strings are then searched for across every persisted surface and
  every structlog event the run emitted;
* **static (the "grep" half)** — an AST scan of ``app/`` asserting no log call
  and no audit ``metadata`` literal names token material, and that the PKCE
  verifier stays inside its two owning modules. Static checks catch the write
  that *would* leak on a code path this suite never happens to execute.

The **callback-stage** INV-6 rows (denied initiation → ``permission.denied``;
failed callback → ``source.connected`` ``outcome=denied|error``; connect and
delete audits) are proven end-to-end against the real API in
``tests/test_connector_oauth.py`` (#452) and are deliberately not duplicated
here — this module owns the sync/credential half.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid

import httpx
import pytest
import structlog
from sqlalchemy import select

import app.db.session as db_session
from app.connectors.oauth import OAuthSpec, TokenResponse
from app.db import models
from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role, SecretKind, SourceStatus
from app.services.audit import AuditSink
from app.services.secrets_service import build_secrets_service

from .engine import FakeEngine
from .subject import AclSubject
from .subjects import SUBJECT_IDS, SUBJECTS
from .sync_harness import (
    FakeObjectStore,
    KitConnector,
    engine_bound_store_factory,
    run_sync,
    settings,
    sync_source_module,
)

pytestmark = pytest.mark.parametrize("subject", SUBJECTS, ids=SUBJECT_IDS)

_APP = pathlib.Path(__file__).resolve().parents[2] / "app"
_CONTRACTS = pathlib.Path(__file__).resolve().parents[3] / "contracts" / "openapi.yaml"

# Distinctive so a substring search cannot false-negative on a common word.
REFRESH_TOKEN = "rt-kit-supersecret-refresh-9f3c1a"
ROTATED_REFRESH_TOKEN = "rt-kit-rotated-refresh-77b210"
ACCESS_TOKEN = "at-kit-supersecret-access-4e8d02"
CLIENT_SECRET = "cs-kit-supersecret-client-1a2b3c"

_TOKEN_MATERIAL = (REFRESH_TOKEN, ROTATED_REFRESH_TOKEN, ACCESS_TOKEN, CLIENT_SECRET)


class OAuthKitConnector(KitConnector):
    """The kit connector plus the OAuth capability (so the vault path runs)."""

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            authorize_url="https://provider.invalid/auth",
            token_url="https://provider.invalid/token",
            scopes=("read",),
            client_id="kit-client",
            client_secret=CLIENT_SECRET,
            allowed_hosts=("provider.invalid",),
        )


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def oauth_connector(
    subject: AclSubject, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> OAuthKitConnector:
    fake = OAuthKitConnector(subject)
    monkeypatch.setattr(sync_source_module, "get_connector", lambda _type: fake)
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", engine_bound_store_factory(engine))
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *a, **k: None)

    async def _refresh(
        _http: httpx.AsyncClient, _spec: OAuthSpec, *, refresh_token: str
    ) -> TokenResponse:
        # The framework already read the credential from the vault to get here —
        # that read is the `secret.accessed` this module asserts on.
        assert refresh_token == REFRESH_TOKEN
        return TokenResponse(
            access_token=ACCESS_TOKEN,
            refresh_token=ROTATED_REFRESH_TOKEN,  # provider rotation on use
            scope="read",
            expires_in=3600,
        )

    monkeypatch.setattr(sync_source_module, "refresh_access_token", _refresh)
    return fake


class _Connected:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    source_id: uuid.UUID
    collection_id: uuid.UUID


async def _seed_connected(subject: AclSubject) -> _Connected:
    """A managed source whose credential really lives in the CC-C vault."""
    seeded = _Connected()
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        users = UserRepository(session, tenant.id)
        owner = await users.create(
            email=subject.attested_email, password_hash="h", roles=[Role.ADMIN]
        )
        await users.attest_email(owner.id, attested_by=owner.id)
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner.id, name="kit"
        )
        vault = build_secrets_service(
            session,
            settings=settings(),
            tenant_id=tenant.id,
            owner_id=owner.id,
            roles=(Role.ADMIN,),
            audit=AuditSink(AuditEventRepository(session, tenant.id)),
            request_id="kit-seed",
            source_ip="test",
        )
        ref = await vault.store_secret(
            name=f"{subject.name} oauth", kind=SecretKind.CONNECTOR_OAUTH, plaintext=REFRESH_TOKEN
        )
        source = await SourceRepository(session, tenant.id).create(
            owner_id=owner.id,
            type=subject.name,
            config={"collection_id": str(collection.id)},
            status=SourceStatus.PENDING,
        )
        bound = await SourceRepository(session, tenant.id).complete_connect(
            source.id,
            expected_generation=source.connect_generation,
            auth_secret_ref=ref.id,
            connected_account={"email": subject.attested_email},
        )
        assert bound is not None
        await session.commit()
    seeded.tenant_id = tenant.id
    seeded.owner_id = owner.id
    seeded.source_id = source.id
    seeded.collection_id = collection.id
    return seeded


async def _audit_rows(tenant_id: uuid.UUID) -> list[models.AuditEvent]:
    async with db_session.session_scope() as session:
        return list(
            (
                await session.execute(
                    select(models.AuditEvent).where(models.AuditEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )


# --- INV-6: the credential read names the system actor ------------------------


async def test_sync_credential_read_is_audited_to_the_system_actor(
    sqlite_db: None, subject: AclSubject, oauth_connector: OAuthKitConnector
) -> None:
    """``secret.accessed`` records **who** read the credential — the platform.

    The sync worker acts as the system under the tenant's authority, so the row
    must be attributable to the system rather than to whichever admin happened
    to own the source (ADR-0019 §1, spec 0004 §2.4).
    """
    seeded = await _seed_connected(subject)
    oauth_connector.full_docs = (("d1", subject.case("public").raw, ()),)
    result = await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert result.status is SourceStatus.READY

    accessed = [
        row for row in await _audit_rows(seeded.tenant_id) if row.action == "secret.accessed"
    ]
    assert accessed, "the vault read emitted no secret.accessed event (INV-6)"
    for row in accessed:
        assert row.actor_id is None, "secret.accessed must not be attributed to a human actor"
        assert row.outcome == "allowed"


async def test_rotated_credential_is_restored_without_leaking_it(
    sqlite_db: None, subject: AclSubject, oauth_connector: OAuthKitConnector
) -> None:
    """A provider-rotated refresh token is re-stored encrypted, in place."""
    seeded = await _seed_connected(subject)
    oauth_connector.full_docs = (("d1", subject.case("public").raw, ()),)
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    async with db_session.session_scope() as session:
        secrets = list((await session.execute(select(models.Secret))).scalars().all())
    assert len(secrets) == 1  # rotated in place, no orphan row
    for token in _TOKEN_MATERIAL:
        assert token.encode() not in secrets[0].ciphertext


# --- the no-token-material guarantee (runtime) --------------------------------


async def test_no_token_material_reaches_any_persisted_surface_or_log(
    sqlite_db: None, subject: AclSubject, oauth_connector: OAuthKitConnector
) -> None:
    """Token/verifier material appears in no row and no log line of a real sync.

    Searches the *serialized* form of everything the run touched — the source
    row (config, connected account, last error), every audit row including its
    metadata, and every structlog event — for the distinctive token strings.
    """
    seeded = await _seed_connected(subject)
    oauth_connector.full_docs = (("d1", subject.case("public").raw, ()),)
    with structlog.testing.capture_logs() as logs:
        await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    async with db_session.session_scope() as session:
        source = (
            await session.execute(select(models.Source).where(models.Source.id == seeded.source_id))
        ).scalar_one()
    surfaces = {
        "sources.config": json.dumps(source.config),
        "sources.connected_account": json.dumps(source.connected_account),
        "sources.last_error": str(source.last_error),
        "audit": json.dumps(
            [
                {
                    "action": row.action,
                    "resource": str(row.resource_id),
                    "metadata": row.event_metadata,
                }
                for row in await _audit_rows(seeded.tenant_id)
            ],
            default=str,
        ),
        "logs": json.dumps(logs, default=str),
    }
    assert surfaces["audit"] != "[]", "the run emitted no audit rows to search"
    for name, blob in surfaces.items():
        for token in _TOKEN_MATERIAL:
            assert token not in blob, f"token material leaked into {name}"


async def test_the_leak_search_is_not_vacuous(
    sqlite_db: None, subject: AclSubject, oauth_connector: OAuthKitConnector
) -> None:
    """Meta-proof: the search above really can find these strings.

    A search that could never match would pass for any implementation, so the
    same needle is planted in the same haystack shape and must be found.
    """
    planted = json.dumps({"config": {"note": REFRESH_TOKEN}})
    assert any(token in planted for token in _TOKEN_MATERIAL)


# --- the no-token-material guarantee (static: the "grep" half) ---------------

# Names that must never be a log keyword or an audit-metadata key. Deliberately
# excludes bare `code` (a legitimate error discriminator throughout the app).
FORBIDDEN_FIELDS = frozenset(
    {
        "refresh_token",
        "access_token",
        "code_verifier",
        "client_secret",
        "authorization_code",
        "auth_code",
        "verifier",
        "plaintext",
        "secret_value",
    }
)
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})


def _python_sources() -> list[pathlib.Path]:
    return sorted(p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_log_call_or_audit_metadata_names_token_material(
    subject: AclSubject,
) -> None:
    """Static scan of ``app/``: nothing logs or audits credential material.

    Catches the leak on a path this suite never executes — a ``log.warning(...,
    refresh_token=tok)`` added in a rarely-hit error branch would pass every
    runtime assertion and fail here.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_log = isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS
            for keyword in node.keywords:
                if is_log and keyword.arg in FORBIDDEN_FIELDS:
                    offenders.append(f"{path.name}:{node.lineno} log kwarg {keyword.arg}")
                if keyword.arg in {"metadata", "event_data"} and isinstance(
                    keyword.value, ast.Dict
                ):
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and key.value in FORBIDDEN_FIELDS:
                            offenders.append(
                                f"{path.name}:{node.lineno} audit metadata key {key.value}"
                            )
    assert not offenders, f"credential material named in a log/audit site: {offenders}"


def test_the_static_scan_detects_a_planted_leak(
    subject: AclSubject, tmp_path: pathlib.Path
) -> None:
    """Meta-proof: the AST scan above is not vacuous."""
    planted = tmp_path / "leaky.py"
    planted.write_text(
        "log.info('oauth.refreshed', refresh_token=token)\n"
        "audit.record(action='x', metadata={'access_token': token})\n",
        encoding="utf-8",
    )
    found: list[str] = []
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_log = isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS
        for keyword in node.keywords:
            if is_log and keyword.arg in FORBIDDEN_FIELDS:
                found.append(str(keyword.arg))
            if keyword.arg == "metadata" and isinstance(keyword.value, ast.Dict):
                found += [
                    str(k.value)
                    for k in keyword.value.keys
                    if isinstance(k, ast.Constant) and k.value in FORBIDDEN_FIELDS
                ]
    assert set(found) == {"refresh_token", "access_token"}


def test_the_pkce_verifier_is_confined_to_its_owning_modules(subject: AclSubject) -> None:
    """The ``code_verifier`` never transits the browser — nor spreads in-repo.

    ADR-0019 §1 keeps every flow binding server-side; containing the identifier
    to the OAuth machinery and its one caller means a new module cannot start
    handling verifier material without this failing.
    """
    allowed = {"oauth.py", "connector_oauth_service.py"}
    touching = {
        path.name
        for path in _python_sources()
        if "code_verifier" in path.read_text(encoding="utf-8")
    }
    assert (
        touching <= allowed
    ), f"code_verifier escaped its chokepoints: {sorted(touching - allowed)}"


def test_the_source_wire_contract_exposes_no_credential_field(subject: AclSubject) -> None:
    """Serialization: the frozen ``Source`` schema has no token-bearing property.

    ``auth_secret_ref`` is a *reference*, and even that is not on the wire — the
    contract is where a well-meaning addition would first show up.
    """
    import yaml

    spec = yaml.safe_load(_CONTRACTS.read_text(encoding="utf-8"))
    source_schema = spec["components"]["schemas"]["Source"]
    properties = set(source_schema.get("properties", {}))
    assert not properties & FORBIDDEN_FIELDS
    assert not {p for p in properties if "token" in p or "secret" in p}, sorted(properties)
