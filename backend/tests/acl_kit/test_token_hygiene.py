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

# Identifiers that denote credential material. A leak is a *value* reaching a
# sink, so the scan looks for these names anywhere in the expression a log call
# or an audit-metadata construction consumes — not only as a keyword label.
# Bare ``code`` is deliberately excluded: it is a legitimate error discriminator
# throughout the app, and taint-tracking it would drown the signal.
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
_METADATA_KEYWORDS = frozenset({"metadata", "event_data", "event_metadata"})


def _python_sources() -> list[pathlib.Path]:
    return sorted(p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts)


def _tainted_identifiers(node: ast.AST) -> set[str]:
    """Every forbidden identifier appearing anywhere inside ``node``.

    Walks the whole sub-expression, so all of these are caught:

    * ``detail=refresh_token`` — a bare :class:`ast.Name` value;
    * ``token=creds.access_token`` — an :class:`ast.Attribute` tail;
    * ``f"failed for {refresh_token}"`` — the name inside an f-string;
    * ``"...".format(code_verifier)`` / ``redact(access_token)`` — a nested call
      argument;
    * ``{"a": refresh_token}`` — a dict *value*, not just a key;
    * a positional argument in any of the above shapes.

    Constants are matched too, so a literal key such as ``{"access_token": v}``
    still trips even when the value itself is opaque.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in FORBIDDEN_FIELDS:
            found.add(child.id)
        elif isinstance(child, ast.Attribute) and child.attr in FORBIDDEN_FIELDS:
            found.add(child.attr)
        elif isinstance(child, ast.Constant) and child.value in FORBIDDEN_FIELDS:
            found.add(str(child.value))
        elif isinstance(child, ast.keyword) and child.arg in FORBIDDEN_FIELDS:
            found.add(child.arg)
    return found


_SCOPE_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


class _ScopeIndex:
    """Lexical scoping for the scan: which binding does a name see *here*?

    Built because keying dict assignments by bare **name** across the whole
    module is wrong in both directions. A later benign ``payload = {...}`` in an
    unrelated function silently overwrote an earlier tainted one (the leak then
    scanned clean), and a tainted binding equally bled into unrelated functions
    that happened to reuse the name (a false positive). Resolution is per
    ``(scope, name)``, nearest preceding assignment first, walking outwards
    through enclosing scopes exactly as Python does — so a closure over a
    tainted payload is still caught while a shadowing local is not.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.owner: dict[int, ast.AST] = {id(tree): tree}
        self.parent: dict[int, ast.AST | None] = {id(tree): None}
        self.dicts: dict[tuple[int, str], list[tuple[int, ast.Dict]]] = {}
        self._index(tree, tree)
        for bindings in self.dicts.values():
            bindings.sort(key=lambda pair: pair[0])

    def _index(self, node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_NODES):
                self.owner[id(child)] = scope
                self.parent[id(child)] = scope
                self._index(child, child)
                continue
            self.owner[id(child)] = scope
            self._record_binding(child, scope)
            self._index(child, scope)

    def _record_binding(self, node: ast.AST, scope: ast.AST) -> None:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Dict):
            return
        for target in targets:
            if isinstance(target, ast.Name):
                self.dicts.setdefault((id(scope), target.id), []).append((node.lineno, value))

    def resolve_dict(self, name: str, call: ast.Call) -> ast.Dict | None:
        """The dict literal ``name`` refers to at ``call``, or ``None``."""
        scope: ast.AST | None = self.owner.get(id(call))
        while scope is not None:
            bindings = self.dicts.get((id(scope), name))
            if bindings:
                preceding = [value for lineno, value in bindings if lineno <= call.lineno]
                # Bound in this scope but only *after* the call: the name is
                # local here, so nothing earlier is visible — unresolvable.
                return preceding[-1] if preceding else None
            scope = self.parent.get(id(scope))
        return None

    def enclosing_function(self, node: ast.AST) -> ast.AST | None:
        """The innermost ``def``/``async def`` around ``node``, if any."""
        scope: ast.AST | None = self.owner.get(id(node))
        while scope is not None and not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = self.parent.get(id(scope))
        return scope


def scan_source(source: str, *, filename: str = "<planted>") -> list[str]:
    """Report every credential-material leak into a log call or audit metadata.

    Extracted as a plain function so the meta-test below can run the **same**
    scanner over deliberately planted leaks — a scanner proven only against the
    code it already passes on is not a proof.
    """
    tree = ast.parse(source, filename=filename)
    index = _ScopeIndex(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        where = f"{filename}:{node.lineno}"
        if isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS:
            # EVERY argument of a log call — positional, keyword, nested, or
            # interpolated — is a sink.
            for argument in [*node.args, *node.keywords]:
                for name in sorted(_tainted_identifiers(argument)):
                    offenders.append(f"{where} log argument names {name}")
        for keyword in node.keywords:
            if keyword.arg not in _METADATA_KEYWORDS:
                continue
            value: ast.AST | None = keyword.value
            if isinstance(value, ast.Name):
                # Follow `metadata=payload` to the binding visible AT THIS CALL,
                # in this lexical scope — never to whichever assignment to that
                # name happened to come last in the file.
                value = index.resolve_dict(value.id, node)
            if value is None:
                # Built somewhere this scan cannot follow. Fall back to the
                # enclosing function: no credential material in scope means
                # nothing for the payload to carry, but an unprovable payload
                # inside a function that DOES handle one is the leak shape.
                scope = index.enclosing_function(node) or tree
                if _tainted_identifiers(scope):
                    offenders.append(
                        f"{where} audit metadata is built indirectly inside a "
                        "function that handles credential material"
                    )
                continue
            for name in sorted(_tainted_identifiers(value)):
                offenders.append(f"{where} audit metadata names {name}")
    return offenders


def test_no_log_call_or_audit_metadata_carries_token_material(subject: AclSubject) -> None:
    """Static scan of ``app/``: no credential material reaches a log or an audit.

    Catches the leak on a path this suite never executes — a ``log.warning(...,
    detail=refresh_token)`` in a rarely-hit error branch would satisfy every
    runtime assertion in this module and fail only here.
    """
    offenders: list[str] = []
    for path in _python_sources():
        offenders += scan_source(path.read_text(encoding="utf-8"), filename=path.name)
    assert not offenders, f"credential material reaches a log/audit sink: {offenders}"


# Each planted line is a shape the FIRST version of this scan let through.
_PLANTED_LEAKS: tuple[tuple[str, str], ...] = (
    ("keyword label", "log.info('e', refresh_token=tok)"),
    ("keyword VALUE", "log.error('refresh failed', detail=refresh_token)"),
    ("attribute value", "log.warning('e', detail=creds.access_token)"),
    ("positional value", "log.info(access_token)"),
    ("f-string interpolation", "log.error(f'failed for {refresh_token}')"),
    ("nested call argument", "log.info('e', detail=redact(code_verifier))"),
    ("inline metadata key", "audit.record(action='x', metadata={'access_token': v})"),
    ("inline metadata VALUE", "audit.record(action='x', metadata={'detail': client_secret})"),
    (
        "indirect metadata dict",
        "payload = {'detail': refresh_token}\naudit.record(action='x', metadata=payload)",
    ),
    (
        "unresolvable metadata inside a credential-handling function",
        "def refresh(refresh_token):\n"
        "    payload = build(refresh_token)\n"
        "    audit.record(action='x', metadata=payload)\n",
    ),
    (
        # A module-wide `name -> dict` map lets a LATER benign assignment to a
        # common local name overwrite an earlier tainted one, and the leak scans
        # clean. Resolution must be per lexical scope.
        "same local name in two scopes, benign assigned last",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n"
        "\n"
        "def benign(name):\n"
        "    payload = {'detail': name}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "same local name shadowed by a benign module-level assignment",
        "payload = {'detail': 'nothing'}\n"
        "\n"
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "rebinding a name to a benign dict AFTER the leaking call",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n"
        "    payload = {'detail': 'scrubbed'}\n",
    ),
)


@pytest.mark.parametrize("shape,leak", _PLANTED_LEAKS, ids=[s for s, _ in _PLANTED_LEAKS])
def test_the_static_scan_detects_each_planted_leak_shape(
    subject: AclSubject, shape: str, leak: str
) -> None:
    """Meta-proof: the scanner catches indirect and positional leaks too.

    The earlier version of this check inspected only keyword *labels* and inline
    metadata keys, so most of these shapes passed. Planting each one keeps the
    scanner honest about what it actually proves.
    """
    assert scan_source(leak), f"the scan misses a {shape} leak: {leak!r}"


def test_metadata_resolution_is_per_scope_not_per_name(subject: AclSubject) -> None:
    """Name resolution is lexical, so a collision must not blur two functions.

    The precision half of the collision proof: with a module-wide map the
    tainted ``payload`` also bled *into* the benign function and flagged it.
    Exactly one call is a leak here, and it is the one in ``leak``.
    """
    collision = (
        "def benign(name):\n"
        "    payload = {'detail': name}\n"
        "    audit.record(metadata=payload)\n"
        "\n"
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n"
    )
    offenders = scan_source(collision, filename="c.py")
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith("c.py:7"), offenders  # the call inside `leak`


def test_a_nested_function_resolves_its_own_binding(subject: AclSubject) -> None:
    """An inner function's own assignment wins over the enclosing one..."""
    nested = (
        "def outer(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "\n"
        "    def inner(name):\n"
        "        payload = {'detail': name}\n"
        "        audit.record(metadata=payload)\n"
        "\n"
        "    return inner\n"
    )
    assert scan_source(nested) == []


def test_a_nested_function_inherits_an_enclosing_binding(subject: AclSubject) -> None:
    """...and a closure over a tainted enclosing binding is still caught."""
    nested = (
        "def outer(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "\n"
        "    def inner():\n"
        "        audit.record(metadata=payload)\n"
        "\n"
        "    return inner\n"
    )
    assert scan_source(nested), "a closed-over tainted payload must still be reported"


def test_the_static_scan_does_not_flag_benign_logging(subject: AclSubject) -> None:
    """...and it is not simply flagging everything.

    A scanner that reported every log call would make the suite green-by-noise
    impossible to maintain and would be silently disabled by the next person.
    """
    benign = (
        "log.info('source_sync.started', source_id=str(source_id))\n"
        "log.warning('oauth.exchange_failed', error=exc.code, status=resp.status_code)\n"
        "audit.record(action='source.connected', metadata={'email': account_email})\n"
        # An indirectly-built payload in a function with no credential material
        # in sight is not evidence of a leak — flagging it would be noise the
        # next maintainer silences by deleting the check.
        "def rename(name):\n"
        "    payload = build(name)\n"
        "    audit.record(action='x', metadata=payload)\n"
    )
    assert scan_source(benign) == []


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
