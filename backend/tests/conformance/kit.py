"""The conformance rules themselves (ADR-0019 §4 / §3 / §2, ADR-0004).

Each function asserts **one** SDK rule and, when it fails, says what the author
must change. They take plain values (never a pytest fixture) so the same rule
can be applied to a real registered connector *and* to a synthetic offender —
which is how ``tests/test_connector_conformance.py`` proves the kit bites.

The rules:

* :func:`check_protocol_surface` — the base protocol is present with the right
  shapes, and any declared capability has the signature the framework calls;
* :func:`check_domain_types_only` — no vendor/SDK type appears in a protocol
  signature (ADR-0004), and :func:`check_domain_documents` re-proves it on the
  values a real offline sync actually produced;
* :func:`check_typed_config_error` — an invalid config raises the typed
  :class:`ConnectorConfigError` with a stable ``code``, never a vendor
  exception; :func:`check_typed_sync_fault` / :func:`check_typed_changes_fault`
  / :func:`check_health_reports_fault` extend the same rule past config, to the
  provider faults a real run actually hits;
* :func:`check_oauth_spec` — the domain ``OAuthSpec`` type, https endpoints,
  non-empty scopes, and a non-empty pinned ``allowed_hosts`` of bare hostnames
  that actually covers the endpoints it declares (URL-parsed, so
  ``https://good.test@evil.test/`` cannot smuggle a host past it);
* :func:`check_cursor_round_trip` / :func:`check_expired_cursor_fallback` — the
  §3 page/cursor contract, including that an expired cursor raises the typed
  signal *before* any page is yielded (the framework's clear-and-full-resync);
  :func:`check_cascade_signal` / :func:`check_incomplete_signal` prove the §3
  cascade signals are actually *produced*, not merely well-typed;
* :func:`check_map_acl_fail_closed` / :func:`check_map_acl_purity` /
  :func:`check_map_acl_never_escalates` — the §2 mapping contract.
"""

from __future__ import annotations

import builtins
import copy
import inspect
import socket
import typing
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from typing import Any, get_args, get_origin
from urllib.parse import urlsplit

from app.connectors.base import (
    AclMappingContext,
    Connector,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHealth,
    CursorExpiredError,
    FetchedDoc,
    PageIntegrity,
    SourceAcl,
    SyncPage,
)
from app.connectors.oauth import OAuthSpec
from app.domain.entities import Source

__all__ = [
    "AclCase",
    "check_cascade_signal",
    "check_cursor_round_trip",
    "check_domain_documents",
    "check_domain_types_only",
    "check_expired_cursor_fallback",
    "check_health_reports_fault",
    "check_incomplete_signal",
    "check_map_acl_fail_closed",
    "check_map_acl_never_escalates",
    "check_map_acl_purity",
    "check_oauth_spec",
    "check_protocol_surface",
    "check_typed_changes_fault",
    "check_typed_config_error",
    "check_typed_sync_fault",
    "declared_capabilities",
]

_GUIDE = "docs/guides/building-a-connector.md"

# The base protocol (ADR-0009 §1) — mandatory for every connector.
_BASE_METHODS: dict[str, tuple[str, ...]] = {
    "validate_config": ("config",),
    "sync": ("source", "run"),
    "health": ("source", "run"),
}

# The optional capabilities (ADR-0019 §4) — checked only when declared.
_CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    "oauth_spec": (),
    "fetch_changes": ("source", "cursor", "run"),
    "map_acl": ("raw", "ctx"),
}

# Methods the framework awaits.
_ASYNC_METHODS = frozenset({"sync", "health"})
# Methods that must stay synchronous (purity: an async map_acl could do I/O).
_SYNC_METHODS = frozenset({"validate_config", "oauth_spec", "map_acl"})

# Modules a protocol signature may name. Everything else — httpx, a vendor SDK,
# a driver type — is the ADR-0004 leak this rule exists to stop.
_DOMAIN_MODULES = frozenset(
    {
        "builtins",
        "types",
        "typing",
        "collections.abc",
        "datetime",
        "uuid",
        "app.connectors.base",
        "app.connectors.oauth",
    }
)
_DOMAIN_MODULE_PREFIXES = ("app.domain",)


def declared_capabilities(connector: object) -> frozenset[str]:
    """Which optional capabilities this connector opts into (presence = opt-in)."""
    return frozenset(
        name for name in _CAPABILITY_METHODS if callable(getattr(connector, name, None))
    )


# --- protocol surface --------------------------------------------------------


def check_protocol_surface(connector: object, *, expected_name: str) -> None:
    """The base protocol + every declared capability has the callable shape the
    framework invokes (ADR-0009 §1, ADR-0019 §4)."""
    name = getattr(connector, "name", None)
    assert isinstance(name, str) and name, (
        f"connector registered as {expected_name!r} has no non-empty `name` attribute — "
        f"`name` is the SourceType key the registry and sources.type use (see {_GUIDE})"
    )
    assert name == expected_name, (
        f"connector `name` is {name!r} but it is registered under {expected_name!r}; "
        "the registry keys off `name`, so the two must agree"
    )

    for method, params in {**_BASE_METHODS, **_CAPABILITY_METHODS}.items():
        attr = getattr(connector, method, None)
        if attr is None and method in _CAPABILITY_METHODS:
            continue  # an undeclared capability is not a defect
        assert callable(attr), (
            f"connector {name!r} is missing `{method}` — the base protocol is "
            f"name / validate_config / sync / health (ADR-0009 §1, {_GUIDE})"
        )
        _check_signature(name, method, attr, params)
        _check_asyncness(name, method, attr)

    assert isinstance(connector, Connector), (
        f"connector {name!r} does not satisfy the runtime-checkable `Connector` "
        f"protocol — check validate_config / sync / health (see {_GUIDE})"
    )


def _check_signature(name: str, method: str, attr: Any, params: tuple[str, ...]) -> None:
    """The framework's exact call must **bind**, not merely name-match.

    Comparing positional parameter names alone accepts
    ``async def sync(self, source, run, *, required)`` — which then raises
    ``TypeError`` the first time the sync task calls ``sync(source, run)``. So
    the check binds a representative call against the real signature: an
    unsatisfied required parameter (keyword-only or otherwise) fails here rather
    than in production.
    """
    try:
        signature = inspect.signature(attr)
    except (TypeError, ValueError) as exc:  # pragma: no cover — exotic callables
        raise AssertionError(
            f"connector {name!r}: `{method}` is not introspectable: {exc}"
        ) from exc
    positional = [
        p.name
        for p in signature.parameters.values()
        if p.kind in {p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD}
    ]
    assert tuple(positional) == params, (
        f"connector {name!r}: `{method}` takes {tuple(positional)} but the framework "
        f"calls it as {method}{tuple(params)} — the execution context is "
        f"framework-supplied, not connector-constructed (ADR-0019 §4, {_GUIDE})"
    )
    sample = tuple(_CallSentinel(param) for param in params)
    try:
        signature.bind(*sample)
    except TypeError as exc:
        raise AssertionError(
            f"connector {name!r}: the framework's call `{method}{tuple(params)}` does not "
            f"bind against this signature ({exc}) — an extra required parameter (a "
            "keyword-only one included) means the framework cannot call the method at "
            f"all; give it a default or drop it ({_GUIDE})"
        ) from exc


@dataclass(frozen=True, slots=True)
class _CallSentinel:
    """A stand-in argument for signature binding — never actually invoked."""

    param: str


def _check_asyncness(name: str, method: str, attr: object) -> None:
    if method in _ASYNC_METHODS:
        assert inspect.iscoroutinefunction(attr), (
            f"connector {name!r}: `{method}` must be `async def` — the framework "
            "awaits it inside the Celery sync task (backend/AGENTS.md: async end-to-end)"
        )
    elif method in _SYNC_METHODS:
        assert not inspect.iscoroutinefunction(attr) and not inspect.isasyncgenfunction(attr), (
            f"connector {name!r}: `{method}` must be a plain synchronous function — "
            "it runs in the request path or must stay pure (ADR-0019 §4)"
        )
    elif method == "fetch_changes":
        assert inspect.isasyncgenfunction(attr) or inspect.iscoroutinefunction(attr), (
            f"connector {name!r}: `fetch_changes` must yield SyncPages "
            "(`async def` generator) — the framework commits one page per "
            f"transaction (ADR-0019 §3, {_GUIDE})"
        )


# --- domain types only (ADR-0004) --------------------------------------------


def check_domain_types_only(connector: object) -> None:
    """No vendor/SDK type appears in a protocol signature (ADR-0004).

    Static half of the boundary rule: a connector is the only code that talks to
    its source, and it hands the framework **domain values** — ``FetchedDoc`` /
    ``SyncPage`` / ``SourceAcl`` / ``ConnectorHealth`` — never an ``httpx``
    response or a vendor client object.
    """
    name = getattr(connector, "name", "<unnamed>")
    for method in (*_BASE_METHODS, *_CAPABILITY_METHODS):
        attr = getattr(connector, method, None)
        if attr is None or not callable(attr):
            continue
        try:
            hints = typing.get_type_hints(attr)
        except Exception as exc:  # pragma: no cover — unresolvable annotation
            raise AssertionError(
                f"connector {name!r}: `{method}` annotations do not resolve ({exc}); "
                "the conformance kit reads them to prove the vendor boundary"
            ) from exc
        for where, hint in hints.items():
            leaks = sorted(
                f"{cls.__module__}.{cls.__qualname__}"
                for cls in _referenced_classes(hint)
                if not _is_domain_class(cls)
            )
            assert not leaks, (
                f"connector {name!r}: `{method}` exposes vendor type(s) {leaks} in "
                f"`{where}` — an adapter returns domain types, never vendor types "
                f"(ADR-0004; map them to FetchedDoc/SyncPage/SourceAcl, {_GUIDE})"
            )


def _referenced_classes(hint: object) -> Iterable[type]:
    """Every concrete class named by a type expression (recursing into args)."""
    origin = get_origin(hint)
    if origin is None:
        if isinstance(hint, type):
            yield hint
        return
    if isinstance(origin, type):
        yield origin
    for arg in get_args(hint):
        yield from _referenced_classes(arg)


def _is_domain_class(cls: type) -> bool:
    module = getattr(cls, "__module__", "")
    return module in _DOMAIN_MODULES or module.startswith(_DOMAIN_MODULE_PREFIXES)


def check_domain_documents(values: Iterable[object], *, connector_name: str, where: str) -> None:
    """Dynamic half: the values an offline sync produced really are domain values.

    Annotations can lie (or be widened); this walks what actually came back.
    """
    for value in values:
        assert type(value) is FetchedDoc, (
            f"connector {connector_name!r}: {where} yielded {type(value).__module__}."
            f"{type(value).__qualname__} — every document must be a FetchedDoc "
            f"domain value (ADR-0004, {_GUIDE})"
        )
        _check_plain_values(value, connector_name=connector_name, where=where)
        assert isinstance(value.title, str) and isinstance(value.text, str)
        assert isinstance(value.url, str)
        assert value.external_id is None or isinstance(value.external_id, str)
        assert value.modified_at is None or isinstance(value.modified_at, datetime)
        assert value.data is None or isinstance(value.data, bytes)
        assert isinstance(value.mime_type, str)
        if value.acl is not None:
            assert type(value.acl) is SourceAcl, (
                f"connector {connector_name!r}: {where} produced a non-SourceAcl acl "
                f"({type(value.acl)!r}) — the mirrored ACL is a domain value"
            )
            assert all(isinstance(p, str) for p in value.acl.principals)
            assert all(isinstance(s, str) for s in value.acl.scope_ids)


def _check_plain_values(doc: FetchedDoc, *, connector_name: str, where: str) -> None:
    """No field of a returned document holds a live vendor object."""
    assert is_dataclass(doc)
    for field in fields(doc):
        value = getattr(doc, field.name)
        if value is None or isinstance(value, str | bytes | int | float | bool | datetime):
            continue
        module = type(value).__module__
        assert module in _DOMAIN_MODULES or module.startswith(_DOMAIN_MODULE_PREFIXES), (
            f"connector {connector_name!r}: {where} returned a document whose "
            f"`{field.name}` is a {module}.{type(value).__qualname__} — a vendor "
            f"object escaped the adapter (ADR-0004, {_GUIDE})"
        )


def check_health_result(health: object, *, connector_name: str) -> None:
    """``health`` answers the domain probe value, never raises a vendor error."""
    assert type(health) is ConnectorHealth, (
        f"connector {connector_name!r}: `health` returned "
        f"{type(health).__module__}.{type(health).__qualname__} — it must answer a "
        f"ConnectorHealth (ADR-0009 §4, {_GUIDE})"
    )
    assert isinstance(health.healthy, bool)
    assert health.detail is None or isinstance(health.detail, str)


# --- typed errors ------------------------------------------------------------


def check_typed_config_error(
    connector: Any, bad_config: Mapping[str, object], *, connector_name: str
) -> None:
    """An invalid config raises the typed error with a stable ``code`` (INV-8).

    The API maps ``ConnectorConfigError.code`` onto the Problem body's ``code``,
    so a vendor exception (or a bare ``ValueError``) escaping here becomes a 500
    instead of a 422 and leaks vendor detail to the client.
    """
    try:
        result = connector.validate_config(dict(bad_config))
    except ConnectorConfigError as exc:
        assert isinstance(exc.code, str) and exc.code, (
            f"connector {connector_name!r}: ConnectorConfigError for {dict(bad_config)!r} "
            "carries no `code` — the code is the stable, machine-readable "
            f"discriminator the API surfaces (ADR-0009 §1, {_GUIDE})"
        )
        assert isinstance(exc.detail, str) and exc.detail
        return
    except ConnectorError as exc:  # typed, but the wrong kind for a bad config
        raise AssertionError(
            f"connector {connector_name!r}: invalid config {dict(bad_config)!r} raised "
            f"{type(exc).__name__} — a permanent rejection of user input must be a "
            "ConnectorConfigError so the API answers 422, not a retryable fault"
        ) from exc
    except Exception as exc:
        raise AssertionError(
            f"connector {connector_name!r}: invalid config {dict(bad_config)!r} raised "
            f"{type(exc).__module__}.{type(exc).__qualname__} — connectors raise the "
            f"typed ConnectorConfigError, never a vendor/builtin exception ({_GUIDE})"
        ) from exc
    raise AssertionError(
        f"connector {connector_name!r}: invalid config {dict(bad_config)!r} was accepted "
        f"(returned {result!r}) — validate_config is the request-path gate that keeps a "
        "bad row from ever being written (INV-8)"
    )


def check_valid_config_round_trip(
    connector: Any, config: Mapping[str, object], *, connector_name: str
) -> None:
    """``validate_config`` returns the JSON-portable config to persist."""
    stored = connector.validate_config(dict(config))
    assert isinstance(stored, dict), (
        f"connector {connector_name!r}: validate_config must return the dict to "
        f"persist in sources.config (got {type(stored)!r})"
    )
    assert all(isinstance(key, str) for key in stored), (
        f"connector {connector_name!r}: validate_config returned non-string keys — "
        "sources.config is portable JSON"
    )


def _assert_typed_fault(exc: BaseException, *, connector_name: str, where: str) -> None:
    """A raised fault is a typed, vendor-free ``ConnectorError`` with a code."""
    assert isinstance(exc, ConnectorError), (
        f"connector {connector_name!r}: {where} raised "
        f"{type(exc).__module__}.{type(exc).__qualname__} — a provider fault must surface "
        "as the typed ConnectorError so the framework records a safe `last_error` and the "
        f"API never leaks a vendor exception or stack trace to a client (ADR-0004, {_GUIDE})"
    )
    assert isinstance(exc.code, str) and exc.code, (
        f"connector {connector_name!r}: the ConnectorError from {where} carries no `code` — "
        "the code is the stable, machine-readable discriminator the framework and the API "
        "key off"
    )
    assert (
        isinstance(exc.detail, str) and exc.detail
    ), f"connector {connector_name!r}: the ConnectorError from {where} carries no `detail`"


async def check_typed_sync_fault(
    connector: Any, *, source: Source, run: Any, connector_name: str
) -> None:
    """A provider fault during ``sync`` is a typed ``ConnectorError``.

    The config rule alone is not the ADR's "typed `ConnectorError` mapping": the
    faults a connector actually meets at 3am are a 500, a dropped connection, a
    malformed body. Those must arrive at the framework typed and vendor-free, or
    the sync task's error handling degrades into "something threw".
    """
    try:
        result = await connector.sync(source, run)
        list(result)
    except BaseException as exc:  # noqa: BLE001 — classifying the fault IS the check
        _assert_typed_fault(exc, connector_name=connector_name, where="sync() on a failing source")
        return
    raise AssertionError(
        f"connector {connector_name!r}: sync() completed against a failing provider — the "
        "harness's fault scenario must actually fault, or this rule proves nothing"
    )


async def check_typed_changes_fault(
    fetch_changes: Callable[[Source, str, Any], AsyncIterator[SyncPage]],
    *,
    source: Source,
    cursor: str,
    run: Any,
    connector_name: str,
) -> None:
    """A provider fault during a replay is a typed ``ConnectorError`` — and is
    **not** ``CursorExpiredError``.

    The distinction is load-bearing: a cursor-expired signal makes the framework
    discard the cursor and resync in full, while an ordinary fault must leave
    the committed cursor alone so the next run resumes where this one stopped.
    Collapsing the two would throw away a valid resume point on every transient
    500.
    """
    try:
        async for _page in fetch_changes(source, cursor, run):
            pass
    except CursorExpiredError as exc:
        raise AssertionError(
            f"connector {connector_name!r}: an ordinary provider fault during a replay "
            "raised CursorExpiredError — that signal means the cursor is dead and makes "
            "the framework discard a still-valid resume point; use ConnectorError for a "
            "transient fault"
        ) from exc
    except BaseException as exc:  # noqa: BLE001 — classifying the fault IS the check
        _assert_typed_fault(
            exc, connector_name=connector_name, where="fetch_changes() on a failing provider"
        )
        return
    raise AssertionError(
        f"connector {connector_name!r}: fetch_changes() drained cleanly against a failing "
        "provider — the harness's fault scenario must actually fault"
    )


async def check_health_reports_fault(
    connector: Any, *, source: Source, run: Any, connector_name: str
) -> None:
    """``health`` **reports** a fault; it never raises one.

    Health is a probe the connector grid renders. A raising probe turns one
    unreachable source into a failed request for the whole grid, so the fault is
    a return value.
    """
    try:
        health = await connector.health(source, run)
    except BaseException as exc:  # noqa: BLE001 — a raise IS the failure being caught
        raise AssertionError(
            f"connector {connector_name!r}: health() raised "
            f"{type(exc).__module__}.{type(exc).__qualname__} against a failing provider — "
            "a probe REPORTS a fault as ConnectorHealth(healthy=False, detail=…) rather "
            f"than raising it ({_GUIDE})"
        ) from exc
    check_health_result(health, connector_name=connector_name)
    assert health.healthy is False, (
        f"connector {connector_name!r}: health() reported healthy against a failing "
        "provider — the harness's fault scenario must actually fault, or the probe is "
        "not really probing"
    )


# --- oauth_spec capability ---------------------------------------------------


def _endpoint_host(url: str, *, connector_name: str, label: str) -> str:
    """The host a *runtime* client would dial — parsed, never string-split.

    String-splitting on ``://`` and ``/`` reads the **userinfo** as the host, so
    ``https://good.test@evil.test/authorize`` looks like it targets
    ``good.test`` while httpx dials ``evil.test``. Pairing that with an
    ``allowed_hosts`` entry of ``"good.test@evil.test"`` would pass a naive
    check and pin nothing. Userinfo is therefore refused outright — an OAuth
    endpoint has no business carrying credentials in its URL.
    """
    parts = urlsplit(url)
    assert parts.scheme == "https", (
        f"connector {connector_name!r}: oauth_spec().{label} is {url!r} — OAuth endpoints "
        "must be https (state/code/tokens never transit cleartext, ADR-0019 §1)"
    )
    assert "@" not in parts.netloc, (
        f"connector {connector_name!r}: oauth_spec().{label} carries userinfo "
        f"({parts.netloc!r}) — the runtime host is {parts.hostname!r}, not what the text "
        "reads like; an endpoint URL must be a plain https://host/path"
    )
    hostname = parts.hostname
    assert hostname, f"connector {connector_name!r}: oauth_spec().{label} has no host ({url!r})"
    return hostname.casefold()


def check_oauth_spec(spec: Any, *, connector_name: str) -> None:
    """The declared OAuth capability is well formed (ADR-0019 §1/§5).

    The domain :class:`OAuthSpec` type (a duck-typed vendor object would carry
    whatever fields it liked past the framework's guard), https endpoints, a
    non-empty scope set, and a non-empty **pinned host set** — the fixed
    allowlist the framework's guarded transport enforces before an Authorization
    header could leave toward anything else. Endpoints and host entries are
    URL-parsed and compared on normalized hosts.
    """
    assert isinstance(spec, OAuthSpec), (
        f"connector {connector_name!r}: oauth_spec() returned "
        f"{type(spec).__module__}.{type(spec).__qualname__}, not the domain OAuthSpec — "
        "the framework builds the authorize URL, the exchange, and the pinned client off "
        f"this exact value object (ADR-0004 domain types, {_GUIDE})"
    )
    scopes = spec.scopes
    allowed_hosts = spec.allowed_hosts

    assert isinstance(scopes, tuple) and scopes and all(isinstance(s, str) and s for s in scopes), (
        f"connector {connector_name!r}: oauth_spec().scopes must be a non-empty tuple "
        f"of scope strings (got {scopes!r}) — the consent screen is built from it"
    )
    assert (
        isinstance(allowed_hosts, tuple)
        and allowed_hosts
        and all(isinstance(h, str) and h for h in allowed_hosts)
    ), (
        f"connector {connector_name!r}: oauth_spec().allowed_hosts is empty — an "
        "authenticated connector run dials ONLY its pinned host set; an empty set "
        f"means every request is refused, and an unpinned one is an SSRF hole "
        f"(ADR-0019 §5 / ADR-0009 §3, {_GUIDE})"
    )

    # Each entry must be a BARE hostname: the guard compares it verbatim against
    # the request's casefolded host, so anything carrying a scheme, userinfo,
    # port, or path silently matches nothing (pinning becomes a no-op) — or
    # worse, reads as a host it is not.
    pinned: set[str] = set()
    for entry in allowed_hosts:
        parsed = urlsplit(f"//{entry}")
        bare = (
            entry == entry.casefold()
            and parsed.hostname == entry
            and parsed.port is None
            and "@" not in entry
            and not any(char in entry for char in "/:?#*")
        )
        assert bare, (
            f"connector {connector_name!r}: allowed_hosts entry {entry!r} must be a bare "
            "lowercase hostname (no scheme, userinfo, port, path, or wildcard) — the "
            "guard compares it verbatim against the request's casefolded host, so a "
            "decorated entry pins nothing"
        )
        pinned.add(entry)

    for label, url in (("authorize_url", spec.authorize_url), ("token_url", spec.token_url)):
        assert (
            isinstance(url, str) and url
        ), f"connector {connector_name!r}: oauth_spec().{label} must be a non-empty string"
        host = _endpoint_host(url, connector_name=connector_name, label=label)
        assert host in pinned, (
            f"connector {connector_name!r}: oauth_spec().{label} resolves to host {host!r}, "
            f"which is not in allowed_hosts {sorted(pinned)} — the guarded client would "
            "refuse the connector's own endpoint"
        )


# --- fetch_changes capability (ADR-0019 §3) ----------------------------------


async def check_cursor_round_trip(
    fetch_changes: Callable[[Source, str, Any], AsyncIterator[SyncPage]],
    *,
    source: Source,
    cursor: str,
    run: Any,
    connector_name: str,
) -> list[SyncPage]:
    """Drain a replay and assert the §3 page/cursor contract.

    Every page is a domain :class:`SyncPage` carrying a non-empty
    ``next_cursor`` — the framework persists exactly that value in the same
    transaction as the page's mutations, so a crash resumes from it. The
    terminal page's cursor is the provider's new baseline; feeding it back in
    must be accepted, which is what makes "resume from the stored cursor" true
    rather than aspirational.
    """
    pages = [page async for page in fetch_changes(source, cursor, run)]
    assert pages, (
        f"connector {connector_name!r}: fetch_changes yielded no page for cursor "
        f"{cursor!r} — the framework needs at least the terminal page to learn the "
        f"new baseline (ADR-0019 §3, {_GUIDE})"
    )
    for index, page in enumerate(pages):
        assert type(page) is SyncPage, (
            f"connector {connector_name!r}: fetch_changes yielded "
            f"{type(page).__module__}.{type(page).__qualname__} at index {index} — "
            "pages are the domain SyncPage (the framework's unit of atomic commit)"
        )
        assert isinstance(page.next_cursor, str) and page.next_cursor, (
            f"connector {connector_name!r}: page {index} has an empty next_cursor — "
            "the cursor commits with the page; an empty one loses the resume point"
        )
        assert isinstance(page.upserts, tuple)
        check_domain_documents(
            page.upserts, connector_name=connector_name, where=f"fetch_changes page {index}"
        )
        assert isinstance(page.deleted_external_ids, frozenset) and all(
            isinstance(i, str) for i in page.deleted_external_ids
        ), f"connector {connector_name!r}: page {index} deleted_external_ids must be frozenset[str]"
        assert isinstance(page.stale_scope_ids, frozenset) and all(
            isinstance(i, str) for i in page.stale_scope_ids
        ), f"connector {connector_name!r}: page {index} stale_scope_ids must be frozenset[str]"
        assert isinstance(page.integrity, PageIntegrity), (
            f"connector {connector_name!r}: page {index} integrity must be a "
            "PageIntegrity — `incomplete` is the fail-closed signal the framework "
            "turns into a source-wide stale stamp (ADR-0019 §3)"
        )

    resumed = [page async for page in fetch_changes(source, pages[-1].next_cursor, run)]
    assert resumed, (
        f"connector {connector_name!r}: the terminal page's next_cursor "
        f"({pages[-1].next_cursor!r}) was not accepted as a resume point — a cursor "
        "the connector emits must be a cursor the connector takes back (ADR-0019 §3)"
    )
    return pages


async def check_expired_cursor_fallback(
    fetch_changes: Callable[[Source, str, Any], AsyncIterator[SyncPage]],
    *,
    source: Source,
    cursor: str,
    run: Any,
    connector_name: str,
) -> None:
    """An invalid/expired cursor raises :class:`CursorExpiredError` — and does so
    **before** any page is yielded.

    That ordering is the contract: the framework clears ``sources.sync_cursor``
    and falls back to a full resync in the same run (ADR-0019 §3). A connector
    that yields a page first would have half-committed a replay the provider has
    already disowned; one that raises a generic error would fail the sync
    instead of self-healing.
    """
    yielded: list[SyncPage] = []
    try:
        async for page in fetch_changes(source, cursor, run):
            yielded.append(page)
    except CursorExpiredError:
        assert not yielded, (
            f"connector {connector_name!r}: fetch_changes yielded {len(yielded)} page(s) "
            "before signalling an expired cursor — the typed signal must come first so "
            "the framework can clear the cursor and resync cleanly (ADR-0019 §3)"
        )
        return
    except ConnectorError as exc:
        raise AssertionError(
            f"connector {connector_name!r}: an invalid/expired cursor raised "
            f"{type(exc).__name__}(code={exc.code!r}) — it must raise the typed "
            "CursorExpiredError, which is what makes the framework clear the cursor "
            f"and fall back to a full resync instead of failing the sync ({_GUIDE})"
        ) from exc
    except Exception as exc:
        raise AssertionError(
            f"connector {connector_name!r}: an invalid/expired cursor raised "
            f"{type(exc).__module__}.{type(exc).__qualname__} — connectors signal it "
            f"with the typed CursorExpiredError ({_GUIDE})"
        ) from exc
    raise AssertionError(
        f"connector {connector_name!r}: an invalid/expired cursor was replayed as if "
        f"valid ({len(yielded)} page(s)) — the provider has disowned that token; raise "
        "CursorExpiredError so the framework resyncs in full (ADR-0019 §3)"
    )


async def check_cascade_signal(
    fetch_changes: Callable[[Source, str, Any], AsyncIterator[SyncPage]],
    *,
    source: Source,
    cursor: str,
    run: Any,
    connector_name: str,
    expected_scope: str | None = None,
) -> None:
    """A container-permission change actually **produces** ``stale_scope_ids``.

    ADR-0019 §4 assigns the connector-side *production* of the cascade signals to
    this kit (the framework's stale-stamp reaction is proven in the sync-task
    tests). Type-checking an empty default proves nothing: providers do not emit
    one change per descendant, so a connector that fails to surface the container
    id leaves every descendant's mirror **fresh** after a permission change —
    a silent fail-open, and the exact bug this field exists to prevent.
    """
    pages = [page async for page in fetch_changes(source, cursor, run)]
    assert pages, f"connector {connector_name!r}: the cascade scenario produced no page at all"
    signalled: set[str] = set()
    for page in pages:
        signalled |= set(page.stale_scope_ids)
    assert signalled, (
        f"connector {connector_name!r}: a container-permission change replayed without "
        "any `stale_scope_ids` — the framework has no way to find the affected "
        "descendants, so their mirrors stay fresh after a permission change (fail-open, "
        f"ADR-0019 §3, {_GUIDE})"
    )
    if expected_scope is not None:
        assert expected_scope in signalled, (
            f"connector {connector_name!r}: expected the changed container "
            f"{expected_scope!r} in stale_scope_ids, got {sorted(signalled)}"
        )


async def check_incomplete_signal(
    fetch_changes: Callable[[Source, str, Any], AsyncIterator[SyncPage]],
    *,
    source: Source,
    cursor: str,
    run: Any,
    connector_name: str,
) -> None:
    """An unprovable page actually **produces** ``integrity=INCOMPLETE``.

    The fail-closed escape hatch (ADR-0019 §3): when the connector cannot prove
    which documents a replayed change affects, it says so and the framework
    stamps the whole source stale. A connector that reports ``COMPLETE`` for a
    page it could not fully interpret is claiming a guarantee it does not have,
    and the framework will believe it.
    """
    pages = [page async for page in fetch_changes(source, cursor, run)]
    assert pages, f"connector {connector_name!r}: the incomplete-page scenario produced no page"
    assert any(page.integrity is PageIntegrity.INCOMPLETE for page in pages), (
        f"connector {connector_name!r}: a replay whose effects cannot be proven complete "
        "reported integrity=COMPLETE — the framework then trusts a guarantee the "
        "connector does not have; emit PageIntegrity.INCOMPLETE so it fails closed "
        f"source-wide (ADR-0019 §3, {_GUIDE})"
    )


# --- map_acl capability (ADR-0019 §2) ----------------------------------------


@dataclass(frozen=True)
class AclCase:
    """One ACL fixture: raw source payload, what the source itself allows, and
    what the mapping must produce.

    ``source_allow`` is the never-escalate reference — the full principal set the
    source's own allow-list could justify. The mapped set must be a **subset**:
    v1 deliberately under-shares (unattested emails, domain shares, unexpanded
    groups all map to nothing), and it may never admit a principal the source
    would deny.
    """

    label: str
    raw: Mapping[str, object]
    source_allow: frozenset[str]
    expected: frozenset[str]


# Degenerate inputs every fail-closed mapper must answer with "nobody".
FAIL_CLOSED_INPUTS: tuple[Mapping[str, object], ...] = (
    {},
    {"permissions": []},
    {"permissions": None},
    {"permissions": "not-a-list"},
    {"permissions": [{}]},
    {"permissions": [None]},
    {"permissions": [{"role": "reader"}]},
    {"permissions": [{"type": "__unknown__", "role": "reader"}]},
    {"permissions": [{"type": "user", "role": "__unknown__", "emailAddress": "x@y.test"}]},
    {"unexpected_shape": True},
)


def check_map_acl_fail_closed(
    map_acl: Callable[[Mapping[str, object], AclMappingContext], frozenset[str]],
    ctx: AclMappingContext,
    *,
    connector_name: str,
) -> None:
    """Unknown or empty input grants **nobody** (ADR-0019 §2).

    Under-sharing is the safe failure direction: an unmappable entry that
    silently became ``tenant`` would expose source-denied content to every member
    of the tenant, and the mirror is the *only* enforcement leg for
    ``acl_enforced`` documents (owner and Lumen grants do not apply).
    """
    for raw in FAIL_CLOSED_INPUTS:
        mapped = map_acl(raw, ctx)
        assert isinstance(mapped, frozenset), (
            f"connector {connector_name!r}: map_acl({dict(raw)!r}) returned "
            f"{type(mapped)!r} — the capability returns frozenset[str] of principals"
        )
        assert mapped == frozenset(), (
            f"connector {connector_name!r}: map_acl({dict(raw)!r}) granted {sorted(mapped)} "
            "— unknown/empty input must grant NOBODY (fail closed, ADR-0019 §2); the "
            f"mirror is the only enforcement leg for these documents ({_GUIDE})"
        )


def check_map_acl_purity(
    map_acl: Callable[[Mapping[str, object], AclMappingContext], frozenset[str]],
    ctx: AclMappingContext,
    raw: Mapping[str, object],
    *,
    connector_name: str,
) -> None:
    """``map_acl`` is pure over the frozen :class:`AclMappingContext`.

    Same input ⇒ same output, no network or file I/O, and **neither input is
    mutated**. The framework freezes an identity snapshot per run precisely so
    mapping is deterministic and replayable — a mapper that dialled a directory
    service, or that edited the raw payload it was handed, would make two
    documents in one sync disagree about who a principal is.

    Enforcement boundary, stated plainly (the guide says the same): this checks
    determinism across repeated calls, immutability of ``raw`` **and** ``ctx``,
    and blocks sockets and :func:`open`. It does **not** intercept the clock —
    a mapper must use ``ctx.evaluated_at`` rather than reading ``datetime.now``,
    and that one stays review-caught.
    """
    params = AclMappingContext.__dataclass_params__  # type: ignore[attr-defined]
    assert is_dataclass(AclMappingContext) and params.frozen, (
        "AclMappingContext must stay a frozen dataclass — map_acl maps against a "
        "snapshot the framework owns for the whole run"
    )

    ctx_before = (dict(ctx.email_to_user_id), ctx.evaluated_at, ctx.tenant_principal)
    raw_before = copy.deepcopy(dict(raw))
    with _no_io(connector_name):
        first = map_acl(raw, ctx)
        second = map_acl(raw, ctx)
    assert first == second, (
        f"connector {connector_name!r}: map_acl is not deterministic — two calls with "
        f"the same input returned {sorted(first)} then {sorted(second)}; the mapping "
        f"must be pure over the frozen AclMappingContext (ADR-0019 §4, {_GUIDE})"
    )
    ctx_after = (dict(ctx.email_to_user_id), ctx.evaluated_at, ctx.tenant_principal)
    assert ctx_before == ctx_after, (
        f"connector {connector_name!r}: map_acl mutated the AclMappingContext — the "
        "identity snapshot is the framework's, shared across the whole run"
    )
    assert raw_before == dict(raw), (
        f"connector {connector_name!r}: map_acl mutated the raw permission payload it was "
        "handed — the mapper reads its input, it does not edit it; the framework may "
        "reuse that payload (health counts, retries) and would then see a different "
        f"document than the source sent ({_GUIDE})"
    )


@contextmanager
def _no_io(connector_name: str) -> typing.Iterator[None]:
    """Make socket *and* file use inside the block an actionable failure.

    The two kinds of I/O a mapper would plausibly reach for: resolving a group
    against a directory service, or reading a mapping file off disk. Both would
    make ACL mapping depend on something outside the frozen snapshot — which is
    to say, non-replayable and not equal across the documents of one sync.
    """

    def _blocked(what: str) -> Callable[..., object]:
        def _raise(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                f"connector {connector_name!r}: map_acl performed {what} — the mapping is "
                "PURE over the frozen AclMappingContext; everything it may know is "
                f"already in that snapshot (ADR-0019 §4, {_GUIDE})"
            )

        return _raise

    saved_socket = (socket.socket, socket.getaddrinfo, socket.create_connection)
    saved_open = builtins.open
    socket.socket = _blocked("network I/O")  # type: ignore[assignment]
    socket.getaddrinfo = _blocked("a DNS lookup")  # type: ignore[assignment]
    socket.create_connection = _blocked("network I/O")  # type: ignore[assignment]
    builtins.open = _blocked("file I/O")  # type: ignore[assignment]
    try:
        yield
    finally:
        builtins.open = saved_open
        socket.socket, socket.getaddrinfo, socket.create_connection = saved_socket  # type: ignore[assignment]


def check_map_acl_never_escalates(
    map_acl: Callable[[Mapping[str, object], AclMappingContext], frozenset[str]],
    ctx: AclMappingContext,
    case: AclCase,
    *,
    connector_name: str,
) -> None:
    """The mapped set is a **subset** of what the source itself allows (INV-2).

    The never-escalate property at conformance level: the system never grants
    access the source would deny. Under-sharing (a smaller set than
    ``source_allow``) is fine and expected in v1; a single principal outside it
    is a privilege escalation through the mirror.
    """
    mapped = map_acl(case.raw, ctx)
    escalated = sorted(mapped - case.source_allow)
    assert not escalated, (
        f"connector {connector_name!r}: map_acl case {case.label!r} granted "
        f"{escalated}, which the source fixture does NOT allow "
        f"({sorted(case.source_allow)}) — the mirror may only ever narrow the "
        f"source's allow-list, never widen it (INV-2 / ADR-0019 §2, {_GUIDE})"
    )
    assert mapped == case.expected, (
        f"connector {connector_name!r}: map_acl case {case.label!r} mapped to "
        f"{sorted(mapped)}, expected {sorted(case.expected)}"
    )
