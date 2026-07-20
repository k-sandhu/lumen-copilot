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
  and no audit ``metadata`` expression names token material, and that the PKCE
  verifier stays inside its two owning modules. Static checks catch the write
  that *would* leak on a code path this suite never happens to execute.

The **callback-stage** INV-6 rows (denied initiation → ``permission.denied``;
failed callback → ``source.connected`` ``outcome=denied|error``; connect and
delete audits) are proven end-to-end against the real API in
``tests/test_connector_oauth.py`` (#452) and are deliberately not duplicated
here — this module owns the sync/credential half.

What the static scan is, and is not
-----------------------------------

It is a **conservative hygiene lint, not sound dataflow analysis.**

Earlier revisions tried to *resolve* ``metadata=<name>`` back to a dict literal
and report only when the result looked tainted. That is a losing design: doing
it correctly means reimplementing Python name resolution **and reachability** in
an AST walker. Binding forms alone (augmented assignment, walrus, tuple/star
unpacking, ``global``/``nonlocal``, comprehension targets, parameters,
``except ... as``) are merely tedious; branch and loop control flow is not
solvable at this size at all — a tainted ``if`` arm followed textually by a
benign ``else``, an ``if False`` rebind, or a rebind in a never-entered loop all
read as "the later assignment wins" to any line-ordered walker. Each patch left
that hole open while making the check *look* more trustworthy, which is worse
than a check that is obviously partial.

So the rule is inverted: **resolve only when provably unambiguous, otherwise
report.** A name resolves only when its scope chain yields exactly one binding,
that binding is a plain ``Assign``/``AnnAssign`` with a literal ``ast.Dict``
right-hand side, and it does not sit inside a branch or loop. Anything else —
several bindings, an unmodelled binding form, a parameter, a name touched by a
``global``/``nonlocal`` declaration anywhere in the file — is emitted as an
``unresolvable`` finding rather than silently assumed clean. Silence now means
*proven clean*; it used to mean *failed to resolve*.

Residual limits, stated plainly:

* **It can over-report.** That is the intended direction: a false positive costs
  one allowlist line with a written reason, a false negative ships a credential
  into an audit record. :data:`UNRESOLVABLE_ALLOWLIST` is that escape hatch and
  is empty on the current tree.
* **It does not track values across calls.** ``_emit(metadata=build(token))``
  is invisible to it; only the expression at the audit/log site is examined.
* **Unresolvable sites in functions that never mention credential material are
  silent**, to keep the noise bounded — see :func:`scan_source`. A credential
  reaching such a function purely through a parameter would not be reported.
* It reasons about **identifiers, not values**: a token renamed to an innocuous
  local before the sink is not detected.

The runtime half above is what covers the paths this suite actually executes;
the two are complementary and neither is claimed to be complete.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

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

# Audit-metadata sites the scan cannot prove clean but a human has. Keyed
# ``file::function::name``; the value is the reason, and an empty one fails
# ``test_every_allowlist_entry_carries_a_reason``. Empty on the current tree —
# every indirect site in ``app/`` today sits in a thin audit-forwarding helper
# that never touches credential material, so the scan stays silent without an
# exemption. Kept (with its tests) so the escape hatch is a reviewed, written
# decision rather than a quiet code change when the first one appears.
UNRESOLVABLE_ALLOWLIST: dict[str, str] = {}


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
_BRANCHING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
# Only this form is resolvable; every other way a name can be bound is recorded
# so it can make the name *unresolvable*, never so it can be trusted.
_RESOLVABLE = "dict_literal"


@dataclass(frozen=True)
class _Binding:
    """One place a name is bound, and whether it is a form we can trust."""

    kind: str
    dict_value: ast.Dict | None
    in_control_flow: bool


class _ScopeIndex:
    """Which binding does a name see here — and is that answer *provable*?

    Deliberately not a name resolver. It records **every** way a name can be
    bound in a scope so that anything short of a single, unconditional,
    literal-dict assignment is reported as unresolvable rather than guessed at.
    See the module docstring for why guessing is the wrong design here.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.owner: dict[int, ast.AST] = {id(tree): tree}
        self.parent: dict[int, ast.AST | None] = {id(tree): None}
        self.bindings: dict[tuple[int, str], list[_Binding]] = {}
        # Any name touched by a `global`/`nonlocal` statement ANYWHERE in the
        # file can be rebound from a scope this walk is not looking at, so it is
        # never resolvable — cheap and blunt on purpose.
        self.rebindable: set[str] = set()
        self._index(tree, tree, ())

    # --- construction ---------------------------------------------------------

    def _index(self, node: ast.AST, scope: ast.AST, stack: tuple[ast.AST, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            self.owner[id(child)] = scope
            if isinstance(child, _SCOPE_NODES):
                self.parent[id(child)] = scope
                self._enter_scope(child, scope, stack)
                self._index(child, child, ())
                continue
            self._record(child, scope, stack)
            self._index(child, scope, (*stack, child))

    def _enter_scope(self, child: ast.AST, scope: ast.AST, stack: tuple[ast.AST, ...]) -> None:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            self._bind(scope, child.name, "def", stack)
            self._bind_params(child.args, child)
        elif isinstance(child, ast.ClassDef):
            self._bind(scope, child.name, "class", stack)
        elif isinstance(child, ast.Lambda):
            self._bind_params(child.args, child)

    def _bind_params(self, args: ast.arguments, scope: ast.AST) -> None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._bind(scope, arg.arg, "parameter", ())
        for optional in (args.vararg, args.kwarg):
            if optional is not None:
                self._bind(scope, optional.arg, "parameter", ())

    def _bind(
        self,
        scope: ast.AST,
        name: str,
        kind: str,
        stack: tuple[ast.AST, ...],
        dict_value: ast.Dict | None = None,
    ) -> None:
        binding = _Binding(
            kind=kind,
            dict_value=dict_value,
            in_control_flow=any(isinstance(a, _BRANCHING) for a in stack),
        )
        self.bindings.setdefault((id(scope), name), []).append(binding)

    def _names(self, target: ast.expr) -> Iterator[ast.Name]:
        """Every ``Name`` a target binds, through tuple/list/star unpacking."""
        if isinstance(target, ast.Name):
            yield target
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                yield from self._names(element)
        elif isinstance(target, ast.Starred):
            yield from self._names(target.value)

    def _record(self, node: ast.AST, scope: ast.AST, stack: tuple[ast.AST, ...]) -> None:
        if isinstance(node, ast.Assign):
            simple = len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            literal = isinstance(node.value, ast.Dict)
            kind = _RESOLVABLE if (simple and literal) else "assignment"
            value = node.value if (simple and literal) else None
            for target in node.targets:
                for name in self._names(target):
                    self._bind(scope, name.id, kind, stack, value)  # type: ignore[arg-type]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            literal = isinstance(node.value, ast.Dict) and isinstance(node.target, ast.Name)
            kind = _RESOLVABLE if literal else "annotated assignment"
            for name in self._names(node.target):
                self._bind(
                    scope,
                    name.id,
                    kind,
                    stack,
                    node.value if literal else None,  # type: ignore[arg-type]
                )
        elif isinstance(node, ast.AugAssign):
            for name in self._names(node.target):
                self._bind(scope, name.id, "augmented assignment", stack)
        elif isinstance(node, ast.NamedExpr):
            self._bind(scope, node.target.id, "walrus", stack)
        elif isinstance(node, ast.For | ast.AsyncFor):
            for name in self._names(node.target):
                self._bind(scope, name.id, "loop target", stack)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for name in self._names(node.optional_vars):
                self._bind(scope, name.id, "with target", stack)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, "except binder", stack)
        elif isinstance(node, ast.comprehension):
            for name in self._names(node.target):
                self._bind(scope, name.id, "comprehension target", stack)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                self.rebindable.add(name)
                self._bind(scope, name, "global/nonlocal declaration", stack)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                self._bind(scope, alias.asname or alias.name.split(".")[0], "import", stack)

    # --- the one query --------------------------------------------------------

    def resolve_dict(self, name: str, call: ast.Call) -> tuple[ast.Dict | None, str | None]:
        """``(dict, None)`` when provably unambiguous, else ``(None, reason)``.

        Walks the scope chain as Python does, with one correction the previous
        revision got wrong: a ``ClassDef`` body is **not** part of the closure
        chain, so a method resolves past its class to the module. Only the
        innermost scope may be a class (code written directly in a class body
        does see class-level names).
        """
        if name in self.rebindable:
            return None, f"{name!r} is rebindable via a global/nonlocal declaration"
        scope: ast.AST | None = self.owner.get(id(call))
        innermost = True
        while scope is not None:
            if isinstance(scope, ast.ClassDef) and not innermost:
                scope = self.parent.get(id(scope))
                continue
            innermost = False
            found = self.bindings.get((id(scope), name))
            if found:
                if len(found) > 1:
                    return None, f"{len(found)} bindings for {name!r} in one scope"
                binding = found[0]
                if binding.kind != _RESOLVABLE or binding.dict_value is None:
                    return None, f"bound by {binding.kind}, not a literal dict"
                if binding.in_control_flow:
                    return None, f"the binding of {name!r} sits inside a branch or loop"
                return binding.dict_value, None
            scope = self.parent.get(id(scope))
        return None, f"no binding for {name!r} in any enclosing scope"

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
            reason: str | None = None
            if isinstance(value, ast.Name):
                value, reason = index.resolve_dict(value.id, node)
            elif not isinstance(value, ast.Dict):
                # A call, comprehension, conditional expression, ... — the value
                # is only knowable at runtime.
                value, reason = None, f"built by {type(keyword.value).__name__}"
            if value is None:
                function = index.enclosing_function(node)
                # Bounded noise: an unresolvable payload in a function that never
                # mentions credential material has nothing to carry. This is a
                # documented limit, not a proof (module docstring).
                if not _tainted_identifiers(function or tree):
                    continue
                site = f"{filename}::{getattr(function, 'name', '<module>')}::{_name_of(keyword)}"
                if site in UNRESOLVABLE_ALLOWLIST:
                    continue
                offenders.append(
                    f"{where} audit metadata is unresolvable ({reason}) in a function "
                    f"that handles credential material — prove it clean or allowlist "
                    f"{site!r} with a reason"
                )
                continue
            for name in sorted(_tainted_identifiers(value)):
                offenders.append(f"{where} audit metadata names {name}")
    return offenders


def _name_of(keyword: ast.keyword) -> str:
    """The identifier a metadata keyword was given, for the allowlist key."""
    if isinstance(keyword.value, ast.Name):
        return keyword.value.id
    return f"<{type(keyword.value).__name__}>"


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
    (
        # The ClassDef body is NOT part of the closure chain, so `m` sees the
        # MODULE binding. Walking the class as a parent found the benign class
        # attribute instead — a false negative.
        "method resolving past a benign class attribute to a tainted module dict",
        "payload = {'detail': refresh_token}\n"
        "\n"
        "class C:\n"
        "    payload = {'detail': 'safe'}\n"
        "\n"
        "    def m(self):\n"
        "        audit.record(metadata=payload)\n",
    ),
)


# Shapes where the value is not knowable statically. Under the previous
# "resolve, then judge" design each of these silently resolved to whichever
# assignment a line-ordered walk happened to reach last, and scanned CLEAN.
# They must now be REPORTED as unresolvable — that is the whole point of the
# inversion, so the assertion checks the reason, not merely that something fired.
_UNRESOLVABLE_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "tainted if-arm followed textually by a benign else",
        "def leak(refresh_token, flag):\n"
        "    if flag:\n"
        "        payload = {'detail': refresh_token}\n"
        "    else:\n"
        "        payload = {'detail': 'safe'}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "unreachable `if False` benign rebind",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    if False:\n"
        "        payload = {'detail': 'safe'}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "benign rebind in a never-entered loop",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    for _ in []:\n"
        "        payload = {'detail': 'safe'}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "same-line benign rebind after the leaking call",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}; audit.record(metadata=payload); "
        "payload = {'detail': 'safe'}\n",
    ),
    (
        "augmented assignment merging in the credential",
        "def leak(refresh_token):\n"
        "    payload = {'detail': 'safe'}\n"
        "    payload |= {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "walrus rebind inside a condition",
        "def leak(refresh_token):\n"
        "    payload = {'detail': 'safe'}\n"
        "    if (payload := {'detail': refresh_token}):\n"
        "        pass\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "tuple unpacking",
        "def leak(refresh_token):\n"
        "    payload, other = {'detail': refresh_token}, 1\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "star unpacking",
        "def leak(refresh_token):\n"
        "    payload, *rest = [{'detail': refresh_token}]\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "global rebind",
        "payload = {'detail': 'safe'}\n"
        "\n"
        "def leak(refresh_token):\n"
        "    global payload\n"
        "    payload = {'detail': refresh_token}\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "nonlocal rebind",
        "def outer(refresh_token):\n"
        "    payload = {'detail': 'safe'}\n"
        "\n"
        "    def inner():\n"
        "        nonlocal payload\n"
        "        payload = {'detail': refresh_token}\n"
        "        audit.record(metadata=payload)\n"
        "\n"
        "    return inner\n",
    ),
    (
        # Comprehension targets get their own scope in Python 3, so this does not
        # really rebind the local. Reporting it is a deliberate over-report — the
        # cheap direction — rather than a modelled scope.
        "comprehension target reusing the name",
        "def leak(refresh_token):\n"
        "    payload = {'detail': 'safe'}\n"
        "    seen = [payload for payload in [{'detail': refresh_token}]]\n"
        "    audit.record(metadata=payload)\n",
    ),
    (
        "lambda parameter shadowing the name",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    emit = lambda payload: audit.record(metadata=payload)\n"
        "    return emit\n",
    ),
    (
        "except binder reusing the name",
        "def leak(refresh_token):\n"
        "    payload = {'detail': refresh_token}\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as payload:\n"
        "        pass\n"
        "    audit.record(metadata=payload)\n",
    ),
)


@pytest.mark.parametrize("shape,leak", _PLANTED_LEAKS, ids=[s for s, _ in _PLANTED_LEAKS])
def test_the_static_scan_detects_each_planted_leak_shape(
    subject: AclSubject, shape: str, leak: str
) -> None:
    """Meta-proof: the scanner catches indirect and positional leaks too.

    The earliest version of this check inspected only keyword *labels* and inline
    metadata keys, so most of these shapes passed. Planting each one keeps the
    scanner honest about what it actually proves.
    """
    assert scan_source(leak), f"the scan misses a {shape} leak: {leak!r}"


@pytest.mark.parametrize(
    "shape,source", _UNRESOLVABLE_SHAPES, ids=[s for s, _ in _UNRESOLVABLE_SHAPES]
)
def test_statically_unknowable_metadata_is_reported_not_assumed_clean(
    subject: AclSubject, shape: str, source: str
) -> None:
    """Every shape a line-ordered resolver got wrong is now a loud report.

    Asserting on the *reason* matters: "something fired" could be satisfied by an
    unrelated log finding, whereas this pins that the scan declined to guess.
    """
    offenders = scan_source(source)
    assert offenders, f"{shape} scanned clean — it must be reported as unresolvable"
    assert any(
        "unresolvable" in offender for offender in offenders
    ), f"{shape} fired, but not as an unresolvable finding: {offenders}"


def test_the_allowlist_silences_exactly_its_own_site(
    subject: AclSubject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch works, and does not silence the site next door.

    The allowlist is empty on the current tree, so without this the machinery
    would be untested until the first real exemption — exactly when a mistake
    would be expensive.
    """
    source = (
        "def leak(refresh_token):\n"
        "    payload = build(refresh_token)\n"
        "    audit.record(metadata=payload)\n"
        "\n"
        "def other(access_token):\n"
        "    payload = build(access_token)\n"
        "    audit.record(metadata=payload)\n"
    )
    assert len(scan_source(source, filename="m.py")) == 2

    monkeypatch.setitem(UNRESOLVABLE_ALLOWLIST, "m.py::leak::payload", "reviewed: build() redacts")
    remaining = scan_source(source, filename="m.py")
    assert len(remaining) == 1, remaining
    assert "m.py::other::payload" in remaining[0]


def test_every_allowlist_entry_carries_a_reason(subject: AclSubject) -> None:
    """An exemption without a written reason is not a decision, it is a hole."""
    unexplained = [site for site, reason in UNRESOLVABLE_ALLOWLIST.items() if not reason.strip()]
    assert not unexplained, f"allowlisted with no reason: {unexplained}"
    for site in UNRESOLVABLE_ALLOWLIST:
        assert site.count("::") == 2, f"allowlist key must be file::function::name, got {site!r}"


def test_allowlist_entries_are_still_needed(subject: AclSubject) -> None:
    """A stale exemption silently widens the check — fail when one is unused.

    Recomputed by scanning ``app/`` with the allowlist disabled and collecting
    the sites that actually report; anything allowlisted but absent from that set
    is dead and must be deleted.
    """
    if not UNRESOLVABLE_ALLOWLIST:
        return
    reported: set[str] = set()
    for path in _python_sources():
        for offender in scan_source(path.read_text(encoding="utf-8"), filename=path.name):
            if "unresolvable" in offender:
                reported.add(offender.split("allowlist ")[-1].split("'")[1])
    stale = set(UNRESOLVABLE_ALLOWLIST) - reported
    assert not stale, f"allowlist entries no longer needed: {sorted(stale)}"


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
