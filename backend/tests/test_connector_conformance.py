"""Connector SDK conformance suite (F-CB-5 #456, ADR-0019 §4).

Parametrized over the **real** registry (:func:`registered_types`), so every
connector that exists must pass, and a connector added tomorrow is enrolled the
moment it is discovered — the SDK's "drop in a package, the framework does the
rest" promise with a matching obligation attached.

What is asserted, per connector:

* the base protocol surface (``name`` / ``validate_config`` / ``sync`` /
  ``health``) with the shapes the framework calls;
* **domain types only** (ADR-0004) — statically in the signatures, and
  dynamically over the documents an offline sync really produced;
* **typed errors** — an invalid config raises ``ConnectorConfigError`` with a
  stable ``code``, never a vendor or builtin exception;
* **the ADR-0019 §4 execution-context prohibitions** — an AST scan of the
  connector package: no secrets service, no DB session/repository, no mutable
  module-level state;
* per **declared capability**: a well-formed ``oauth_spec()`` (https endpoints,
  non-empty scopes, non-empty pinned ``allowed_hosts``); ``fetch_changes``
  cursor round-trip + the typed expired-cursor fallback; and ``map_acl``
  fail-closed / pure / never-escalating behaviour.

The second half of the file is the proof that the kit **bites**: synthetic
connectors that break exactly one rule each, asserted to fail with the message
that tells the author what to fix. A kit nobody can see fail is a kit nobody
should trust.

The rules themselves live in ``tests/conformance/`` so both halves apply the
identical check — see ``docs/guides/building-a-connector.md``.
"""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.connectors.base import (
    AclMappingContext,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHealth,
    ConnectorRun,
    CursorExpiredError,
    FetchedDoc,
    FullSyncResult,
    PageIntegrity,
    SourceAcl,
    SyncPage,
    get_fetch_changes,
    get_map_acl,
    get_oauth_spec,
)
from app.connectors.oauth import OAuthSpec
from app.connectors.registry import get_connector, registered_types
from app.domain.entities import Source
from tests.conformance import kit
from tests.conformance.harnesses import ConnectorHarness, harness_for, harness_names
from tests.conformance.prohibitions import (
    Violation,
    check_execution_context_prohibitions,
    connector_package_path,
    scan_package,
)

# Every connector the registry discovers — the suite is parametrized over the
# real thing, never a curated list (a curated list is how a connector skips).
CONNECTOR_TYPES = sorted(registered_types())


@pytest.fixture(params=CONNECTOR_TYPES)
def harness(request: pytest.FixtureRequest) -> ConnectorHarness:
    return harness_for(str(request.param))


# --- the kit covers whatever the registry discovered -------------------------


def test_registry_is_non_empty() -> None:
    """Guard against a vacuous pass: a broken registry would make every
    parametrized test below disappear rather than fail."""
    assert CONNECTOR_TYPES, "the connector registry discovered nothing — the kit would be vacuous"
    assert {"web", "gdrive"} <= set(CONNECTOR_TYPES)


def test_every_registered_connector_has_a_harness() -> None:
    """A connector may not opt out of conformance by omitting its fixtures."""
    missing = sorted(set(CONNECTOR_TYPES) - harness_names())
    assert not missing, (
        f"registered connector(s) {missing} have no conformance harness — add one in "
        "backend/tests/conformance/harnesses.py (see docs/guides/building-a-connector.md)"
    )


# --- base protocol -----------------------------------------------------------


@pytest.mark.parametrize("name", CONNECTOR_TYPES)
def test_protocol_surface(name: str) -> None:
    """``name`` / ``validate_config`` / ``sync`` / ``health`` in the right shapes."""
    kit.check_protocol_surface(get_connector(name), expected_name=name)


@pytest.mark.parametrize("name", CONNECTOR_TYPES)
def test_signatures_expose_domain_types_only(name: str) -> None:
    """ADR-0004: no vendor/SDK type appears in a protocol signature."""
    kit.check_domain_types_only(get_connector(name))


def test_invalid_config_raises_typed_error(harness: ConnectorHarness) -> None:
    """INV-8: a bad config is a typed ``ConnectorConfigError`` with a stable code."""
    connector = get_connector(harness.name)
    assert harness.invalid_configs, (
        f"harness for {harness.name!r} declares no invalid config — the typed-error "
        "rule cannot be proven without at least one rejectable input"
    )
    for bad in harness.invalid_configs:
        kit.check_typed_config_error(connector, bad, connector_name=harness.name)


def test_valid_config_round_trips(harness: ConnectorHarness) -> None:
    """``validate_config`` returns the portable JSON config to persist."""
    kit.check_valid_config_round_trip(
        get_connector(harness.name), harness.valid_config, connector_name=harness.name
    )


async def test_sync_returns_domain_documents(
    harness: ConnectorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dynamic half of ADR-0004: a real offline sync yields FetchedDocs only.

    Also pins the §2 write-mode derivation at its source: a ``map_acl``-declaring
    connector must attach a :class:`SourceAcl` to every document it emits,
    because that presence is what makes the framework write the document
    ``acl_enforced=true`` — the mode is structural, never defaulted, so a
    document that arrives without a mirror would silently fall back to the
    owner/grants leg that must not apply to connector content.
    """
    connector = get_connector(harness.name)
    async with harness.run(monkeypatch) as run:
        result = await connector.sync(harness.source(), run)
    docs = list(result)
    assert docs, f"connector {harness.name!r}: the harness sync produced no documents"
    kit.check_domain_documents(docs, connector_name=harness.name, where="sync()")

    if get_map_acl(connector) is not None:
        unmirrored = [d.title for d in docs if d.acl is None]
        assert not unmirrored, (
            f"connector {harness.name!r} declares map_acl but emitted document(s) "
            f"{unmirrored} with no SourceAcl — the acl_enforced write mode is derived "
            "from the mirror's presence (ADR-0019 §2); an unmirrored document would be "
            "written under the owner/grants leg that must not apply to connector content"
        )


async def test_health_returns_domain_probe(
    harness: ConnectorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``health`` answers a ``ConnectorHealth``, never raises a vendor error."""
    connector = get_connector(harness.name)
    async with harness.run(monkeypatch) as run:
        health = await connector.health(harness.source(), run)
    kit.check_health_result(health, connector_name=harness.name)


# --- execution-context prohibitions (ADR-0019 §4) ----------------------------


@pytest.mark.parametrize("name", CONNECTOR_TYPES)
def test_execution_context_prohibitions(name: str) -> None:
    """No vault, no DB, no mutable module state — pinned structurally.

    The ADR's own words: *"connectors never read the vault, the DB, or mutable
    module state; the conformance kit pins these prohibitions"*. Behavioural
    tests cannot prove this (a connector that resolves its own credential passes
    every green path), so the check is an AST scan of the whole package.
    """
    check_execution_context_prohibitions(connector_package_path(name))


# --- capability: oauth_spec --------------------------------------------------


@pytest.mark.parametrize("name", CONNECTOR_TYPES)
def test_oauth_spec_is_wellformed(name: str) -> None:
    """A declared OAuth capability pins https endpoints, scopes, and its hosts."""
    spec = get_oauth_spec(get_connector(name))
    if spec is None:
        pytest.skip(f"{name} declares no oauth_spec capability")
    kit.check_oauth_spec(spec, connector_name=name)


def test_at_least_one_connector_declares_each_capability() -> None:
    """Sanity: the capability rules are not all skipping into a vacuum."""
    declared: dict[str, list[str]] = {"oauth_spec": [], "fetch_changes": [], "map_acl": []}
    for name in CONNECTOR_TYPES:
        for capability in kit.declared_capabilities(get_connector(name)):
            declared[capability].append(name)
    unproven = sorted(c for c, names in declared.items() if not names)
    assert not unproven, f"no registered connector declares {unproven} — those rules prove nothing"


# --- capability: fetch_changes (ADR-0019 §3) ---------------------------------


async def test_cursor_round_trip(
    harness: ConnectorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page shape + the cursor a connector emits is a cursor it takes back."""
    connector = get_connector(harness.name)
    fetch_changes = get_fetch_changes(connector)
    if fetch_changes is None:
        pytest.skip(f"{harness.name} declares no fetch_changes capability")
    assert harness.start_cursor is not None, (
        f"harness for {harness.name!r} declares fetch_changes but no start_cursor — "
        "the §3 cursor contract cannot be exercised without a replay fixture"
    )
    async with harness.run(monkeypatch) as run:
        await kit.check_cursor_round_trip(
            fetch_changes,
            source=harness.source(),
            cursor=harness.start_cursor,
            run=run,
            connector_name=harness.name,
        )


async def test_expired_cursor_falls_back(
    harness: ConnectorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid/expired cursor raises the typed signal before any page."""
    connector = get_connector(harness.name)
    fetch_changes = get_fetch_changes(connector)
    if fetch_changes is None:
        pytest.skip(f"{harness.name} declares no fetch_changes capability")
    assert harness.expired_cursor is not None, (
        f"harness for {harness.name!r} declares fetch_changes but no expired_cursor "
        "fixture — the clear-and-full-resync fallback is part of the contract"
    )
    async with harness.run(monkeypatch) as run:
        await kit.check_expired_cursor_fallback(
            fetch_changes,
            source=harness.source(),
            cursor=harness.expired_cursor,
            run=run,
            connector_name=harness.name,
        )


# --- capability: map_acl (ADR-0019 §2) ---------------------------------------


def test_map_acl_fails_closed(harness: ConnectorHarness) -> None:
    """Unknown/empty input grants nobody."""
    map_acl = get_map_acl(get_connector(harness.name))
    if map_acl is None:
        pytest.skip(f"{harness.name} declares no map_acl capability")
    assert harness.acl_context is not None
    kit.check_map_acl_fail_closed(map_acl, harness.acl_context, connector_name=harness.name)


def test_map_acl_is_pure(harness: ConnectorHarness) -> None:
    """Deterministic over the frozen snapshot, no I/O, snapshot left untouched."""
    map_acl = get_map_acl(get_connector(harness.name))
    if map_acl is None:
        pytest.skip(f"{harness.name} declares no map_acl capability")
    assert harness.acl_context is not None and harness.acl_cases
    for case in harness.acl_cases:
        kit.check_map_acl_purity(
            map_acl, harness.acl_context, case.raw, connector_name=harness.name
        )


def test_map_acl_never_escalates(harness: ConnectorHarness) -> None:
    """INV-2 at conformance level: the mirror only ever narrows the source's list."""
    map_acl = get_map_acl(get_connector(harness.name))
    if map_acl is None:
        pytest.skip(f"{harness.name} declares no map_acl capability")
    assert harness.acl_context is not None
    assert harness.acl_cases, (
        f"harness for {harness.name!r} declares map_acl but no ACL cases — the "
        "never-escalate property needs at least one source fixture to be a subset of"
    )
    for case in harness.acl_cases:
        kit.check_map_acl_never_escalates(
            map_acl, harness.acl_context, case, connector_name=harness.name
        )


# =============================================================================
# The kit bites — synthetic offenders, one broken rule each.
# =============================================================================

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
_CTX = AclMappingContext(email_to_user_id={"alice@acme.test": uuid.uuid4()}, evaluated_at=_NOW)


class _GoodConnector:
    """A minimal conformant connector — the control for the offenders below."""

    name = "synthetic"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        if not config.get("ok"):
            raise ConnectorConfigError("needs ok", code="invalid_synthetic")
        return {"ok": True}

    async def sync(self, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]:
        return [FetchedDoc(title="t", text="x", url="https://example.test/x")]

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        return ConnectorHealth(healthy=True)


def test_control_connector_passes_the_kit() -> None:
    """The offenders below differ from this one by exactly one broken rule."""
    kit.check_protocol_surface(_GoodConnector(), expected_name="synthetic")
    kit.check_domain_types_only(_GoodConnector())
    kit.check_typed_config_error(_GoodConnector(), {}, connector_name="synthetic")


class _MissingHealthConnector:
    """Base protocol incomplete — the capability-method omission case."""

    name = "synthetic"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return config

    async def sync(self, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]:
        return []


def test_kit_bites_missing_protocol_method() -> None:
    with pytest.raises(AssertionError, match=r"missing `health`"):
        kit.check_protocol_surface(_MissingHealthConnector(), expected_name="synthetic")


class _SyncSyncConnector(_GoodConnector):
    """``sync`` is not awaitable — the framework awaits it."""

    def sync(self, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]:  # type: ignore[override]
        return []


def test_kit_bites_non_async_sync() -> None:
    with pytest.raises(AssertionError, match=r"`sync` must be `async def`"):
        kit.check_protocol_surface(_SyncSyncConnector(), expected_name="synthetic")


class _WrongSignatureConnector(_GoodConnector):
    """``sync`` builds its own context instead of taking the framework's."""

    async def sync(self, source: Source) -> Iterable[FetchedDoc]:  # type: ignore[override]
        return []


def test_kit_bites_wrong_signature() -> None:
    with pytest.raises(AssertionError, match=r"the execution context is framework-supplied"):
        kit.check_protocol_surface(_WrongSignatureConnector(), expected_name="synthetic")


class _VendorLeakConnector(_GoodConnector):
    """Returns the vendor's own response objects (the ADR-0004 leak)."""

    async def sync(self, source: Source, run: ConnectorRun) -> list[httpx.Response]:  # type: ignore[override]
        return []


def test_kit_bites_vendor_type_in_signature() -> None:
    with pytest.raises(AssertionError, match=r"httpx\.Response"):
        kit.check_domain_types_only(_VendorLeakConnector())


class _VendorErrorConnector(_GoodConnector):
    """Lets a builtin/vendor exception escape ``validate_config``."""

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        raise ValueError("bad url")


def test_kit_bites_untyped_config_error() -> None:
    with pytest.raises(AssertionError, match=r"never a vendor/builtin exception"):
        kit.check_typed_config_error(_VendorErrorConnector(), {}, connector_name="synthetic")


class _CodelessErrorConnector(_GoodConnector):
    """Typed error, but no stable ``code`` for the API to surface."""

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        raise ConnectorConfigError("nope", code="")


def test_kit_bites_codeless_config_error() -> None:
    with pytest.raises(AssertionError, match=r"carries no `code`"):
        kit.check_typed_config_error(_CodelessErrorConnector(), {}, connector_name="synthetic")


class _PermissiveConfigConnector(_GoodConnector):
    """Accepts everything — the request-path gate that never closes."""

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return dict(config)


def test_kit_bites_config_that_accepts_anything() -> None:
    with pytest.raises(AssertionError, match=r"was accepted"):
        kit.check_typed_config_error(_PermissiveConfigConnector(), {}, connector_name="synthetic")


@pytest.mark.parametrize(
    "spec,expected",
    [
        (
            OAuthSpec(
                authorize_url="http://provider.test/authorize",
                token_url="https://provider.test/token",
                scopes=("read",),
                client_id="c",
                client_secret="s",
                allowed_hosts=("provider.test",),
            ),
            r"must be https",
        ),
        (
            OAuthSpec(
                authorize_url="https://provider.test/authorize",
                token_url="https://provider.test/token",
                scopes=(),
                client_id="c",
                client_secret="s",
                allowed_hosts=("provider.test",),
            ),
            r"scopes must be a non-empty tuple",
        ),
        (
            OAuthSpec(
                authorize_url="https://provider.test/authorize",
                token_url="https://provider.test/token",
                scopes=("read",),
                client_id="c",
                client_secret="s",
                allowed_hosts=(),
            ),
            r"allowed_hosts is empty",
        ),
        (
            OAuthSpec(
                authorize_url="https://provider.test/authorize",
                token_url="https://tokens.provider.test/token",
                scopes=("read",),
                client_id="c",
                client_secret="s",
                allowed_hosts=("provider.test",),
            ),
            r"not in allowed_hosts",
        ),
        (
            OAuthSpec(
                authorize_url="https://provider.test/authorize",
                token_url="https://provider.test/token",
                scopes=("read",),
                client_id="c",
                client_secret="s",
                allowed_hosts=("https://provider.test/",),
            ),
            r"must be a bare lowercase hostname",
        ),
    ],
)
def test_kit_bites_malformed_oauth_spec(spec: OAuthSpec, expected: str) -> None:
    with pytest.raises(AssertionError, match=expected):
        kit.check_oauth_spec(spec, connector_name="synthetic")


def _escalating_map_acl(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
    """Maps an unexpanded group to the whole tenant — the classic escalation."""
    return frozenset({"tenant"})


def test_kit_bites_escalating_map_acl() -> None:
    """The AC's explicit negative: a non-subset of the source allow-list fails."""
    case = kit.AclCase(
        label="group-only share",
        raw={"permissions": [{"type": "group", "role": "reader", "emailAddress": "t@acme.test"}]},
        source_allow=frozenset({"user:someone"}),
        expected=frozenset(),
    )
    with pytest.raises(AssertionError, match=r"may only ever narrow the source's allow-list"):
        kit.check_map_acl_never_escalates(
            _escalating_map_acl, _CTX, case, connector_name="synthetic"
        )


def test_kit_bites_fail_open_map_acl() -> None:
    with pytest.raises(AssertionError, match=r"must grant NOBODY"):
        kit.check_map_acl_fail_closed(_escalating_map_acl, _CTX, connector_name="synthetic")


def test_kit_bites_impure_map_acl() -> None:
    calls = {"n": 0}

    def _impure(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
        calls["n"] += 1
        return frozenset({f"user:{calls['n']}"})

    with pytest.raises(AssertionError, match=r"not deterministic"):
        kit.check_map_acl_purity(_impure, _CTX, {}, connector_name="synthetic")


def test_kit_bites_map_acl_that_dials_out() -> None:
    def _dialling(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
        import socket

        socket.getaddrinfo("directory.example.test", 443)
        return frozenset()  # pragma: no cover — the lookup raises first

    with pytest.raises(AssertionError, match=r"map_acl opened a socket"):
        kit.check_map_acl_purity(_dialling, _CTX, {}, connector_name="synthetic")


def test_kit_bites_map_acl_that_mutates_the_snapshot() -> None:
    def _mutating(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
        mapping = ctx.email_to_user_id
        assert isinstance(mapping, dict)
        mapping["injected@acme.test"] = uuid.uuid4()
        return frozenset()

    ctx = AclMappingContext(email_to_user_id={"alice@acme.test": uuid.uuid4()}, evaluated_at=_NOW)
    with pytest.raises(AssertionError, match=r"mutated the AclMappingContext"):
        kit.check_map_acl_purity(_mutating, ctx, {}, connector_name="synthetic")


def _stub_source() -> Source:
    from app.domain.entities import SourceStatus

    return Source(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        type="synthetic",
        config={},
        status=SourceStatus.PENDING,
        indexed_count=0,
        last_synced_at=None,
        last_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_kit_bites_cursor_that_never_comes_back() -> None:
    """A connector whose terminal cursor is not a valid resume point."""

    async def _one_shot(source: Source, cursor: str, run: Any) -> AsyncIterator[SyncPage]:
        if cursor != "start":
            return
        yield SyncPage(upserts=(), deleted_external_ids=frozenset(), next_cursor="dead-end")

    with pytest.raises(AssertionError, match=r"was not accepted as a resume point"):
        await kit.check_cursor_round_trip(
            _one_shot, source=_stub_source(), cursor="start", run=None, connector_name="synthetic"
        )


async def test_kit_bites_empty_cursor() -> None:
    async def _empty_cursor(source: Source, cursor: str, run: Any) -> AsyncIterator[SyncPage]:
        yield SyncPage(upserts=(), deleted_external_ids=frozenset(), next_cursor="")

    with pytest.raises(AssertionError, match=r"empty next_cursor"):
        await kit.check_cursor_round_trip(
            _empty_cursor,
            source=_stub_source(),
            cursor="start",
            run=None,
            connector_name="synthetic",
        )


async def test_kit_bites_untyped_expired_cursor() -> None:
    """A generic error instead of ``CursorExpiredError`` fails the sync rather
    than triggering the framework's clear-and-full-resync."""

    async def _generic_error(source: Source, cursor: str, run: Any) -> AsyncIterator[SyncPage]:
        raise ConnectorError("410 gone", code="drive_api_error")
        yield  # pragma: no cover — unreachable, keeps this an async generator

    with pytest.raises(AssertionError, match=r"must raise the typed\s+CursorExpiredError"):
        await kit.check_expired_cursor_fallback(
            _generic_error,
            source=_stub_source(),
            cursor="expired",
            run=None,
            connector_name="synthetic",
        )


async def test_kit_bites_page_yielded_before_cursor_expiry() -> None:
    async def _late_signal(source: Source, cursor: str, run: Any) -> AsyncIterator[SyncPage]:
        yield SyncPage(upserts=(), deleted_external_ids=frozenset(), next_cursor="c1")
        raise CursorExpiredError()

    with pytest.raises(AssertionError, match=r"before signalling an expired cursor"):
        await kit.check_expired_cursor_fallback(
            _late_signal,
            source=_stub_source(),
            cursor="expired",
            run=None,
            connector_name="synthetic",
        )


async def test_kit_bites_expired_cursor_replayed_as_valid() -> None:
    async def _pretends_valid(source: Source, cursor: str, run: Any) -> AsyncIterator[SyncPage]:
        yield SyncPage(upserts=(), deleted_external_ids=frozenset(), next_cursor="c1")

    with pytest.raises(AssertionError, match=r"was replayed as if\s+valid"):
        await kit.check_expired_cursor_fallback(
            _pretends_valid,
            source=_stub_source(),
            cursor="expired",
            run=None,
            connector_name="synthetic",
        )


def test_kit_bites_vendor_object_in_a_returned_document() -> None:
    """Annotations can be right while the values are not."""
    leaked = FetchedDoc(
        title="t", text="x", url="https://example.test/x", data=None, mime_type="text/plain"
    )
    object.__setattr__(leaked, "acl", httpx.Headers())
    with pytest.raises(AssertionError, match=r"non-SourceAcl acl|vendor object escaped"):
        kit.check_domain_documents([leaked], connector_name="synthetic", where="sync()")


def test_kit_bites_non_domain_document() -> None:
    with pytest.raises(AssertionError, match=r"must be a FetchedDoc"):
        kit.check_domain_documents(
            [httpx.Response(200)], connector_name="synthetic", where="sync()"
        )


def test_kit_bites_missing_harness() -> None:
    with pytest.raises(AssertionError, match=r"no conformance harness"):
        harness_for("not-a-registered-connector")


# --- the prohibition scan bites too ------------------------------------------

_CONFORMANT_MODULE = '''
"""A conformant connector package body."""
from app.connectors.base import ConnectorHealth
from app.core.config import get_settings

_EXPORT_MIME = {"a": "b"}
__all__ = ["CONNECTOR"]

def helper(items: list[str]) -> list[str]:
    local = []
    local.append(items[0])
    return local
'''

_OFFENDERS: dict[str, tuple[str, str]] = {
    "imports the vault": (
        "from app.services.secrets_service import SecretsService\n",
        "credential vault",
    ),
    "imports a repository": (
        "from app.db.repositories import SourceRepository\n",
        "connectors never read or write the DB",
    ),
    "imports the DB toolkit": (
        "import sqlalchemy\n",
        "private engine inside a connector",
    ),
    "imports the vault lazily inside a function": (
        "def go() -> None:\n    from app.services import secrets_service\n",
        "credential vault",
    ),
    "holds a module-level cache": (
        "_token_cache = {}\n",
        "module-level mutable container",
    ),
    "mutates a module-level constant": (
        "SEEN = set()\n\ndef go() -> None:\n    SEEN.add('x')\n",
        "mutates module state",
    ),
    "rebinds module state with global": (
        "COUNT = 0\n\ndef go() -> None:\n    global COUNT\n    COUNT = 1\n",
        "rebinds module state",
    ),
    "assigns into a module-level table": (
        "TABLE = dict()\n\ndef go() -> None:\n    TABLE['k'] = 'v'\n",
        "writes to module-level `TABLE`",
    ),
}


def _write_package(root: Path, body: str) -> Path:
    package = root / "synthetic_connector"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("CONNECTOR = None\n", encoding="utf-8")
    (package / "connector.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return package


def test_prohibition_scan_passes_a_conformant_package(tmp_path: Path) -> None:
    """The control: reading config, constant tables, and local mutation are fine."""
    assert scan_package(_write_package(tmp_path, _CONFORMANT_MODULE)) == []


@pytest.mark.parametrize("label", sorted(_OFFENDERS))
def test_prohibition_scan_bites(label: str, tmp_path: Path) -> None:
    """Each §4 prohibition fails with a message naming the fix."""
    body, expected = _OFFENDERS[label]
    package = _write_package(tmp_path, body)
    violations: list[Violation] = scan_package(package)
    assert violations, f"the prohibition scan missed: {label}"
    assert any(expected in v.detail for v in violations), (
        f"{label}: expected a message containing {expected!r}, got "
        f"{[v.detail for v in violations]}"
    )
    with pytest.raises(AssertionError, match=r"ADR-0019 §4 execution-context"):
        check_execution_context_prohibitions(package)


def test_real_connectors_are_scanned_not_skipped() -> None:
    """Guard the scan itself: it must actually find and parse real modules.

    A path bug (empty package, wrong root) would make every prohibition test
    above pass by scanning nothing.
    """
    for name in CONNECTOR_TYPES:
        package = connector_package_path(name)
        modules = sorted(p.name for p in package.rglob("*.py"))
        assert "__init__.py" in modules, f"{name}: no package body found at {package}"
        assert len(modules) >= 2, f"{name}: only {modules} scanned — the connector body is missing"


# --- the framework's own contract the guide documents ------------------------


def test_capability_getters_are_presence_based() -> None:
    """Capability detection is duck-typed off the object (ADR-0019 §4).

    The guide tells authors "add the method, the framework picks it up" — this
    is that promise, pinned: no registry edit, no declaration list.
    """
    web = get_connector("web")
    gdrive = get_connector("gdrive")
    assert get_oauth_spec(web) is None and get_oauth_spec(gdrive) is not None
    assert get_fetch_changes(web) is None and get_fetch_changes(gdrive) is not None
    assert get_map_acl(web) is None and get_map_acl(gdrive) is not None
    assert kit.declared_capabilities(web) == frozenset()
    assert kit.declared_capabilities(gdrive) == frozenset(
        {"oauth_spec", "fetch_changes", "map_acl"}
    )


async def test_full_sync_result_is_iterable_as_the_base_protocol() -> None:
    """``FullSyncResult`` satisfies ``sync``'s ``Iterable[FetchedDoc]`` contract.

    The compatibility seam the guide points at: a capability-declaring connector
    can carry the baseline cursor back without breaking a base-protocol caller.
    """
    doc = FetchedDoc(title="t", text="x", url="https://example.test/x")
    result = FullSyncResult(docs=(doc,), baseline_cursor="token-1", skipped_count=2)
    assert list(result) == [doc]
    kit.check_domain_documents(result, connector_name="synthetic", where="FullSyncResult")


def test_acl_declaring_connector_marks_documents_enforced() -> None:
    """A ``map_acl`` connector's documents carry a ``SourceAcl`` — the structural
    derivation of ``acl_enforced`` at the write seam (ADR-0019 §2)."""
    doc = FetchedDoc(
        title="t",
        text="x",
        url="https://example.test/x",
        acl=SourceAcl(principals=frozenset({"tenant"}), scope_ids=frozenset({"drive-1"})),
    )
    assert doc.acl is not None
    assert doc.acl.principals == frozenset({"tenant"})
    assert (
        SyncPage(upserts=(doc,), deleted_external_ids=frozenset(), next_cursor="c").integrity
        is PageIntegrity.COMPLETE
    )
