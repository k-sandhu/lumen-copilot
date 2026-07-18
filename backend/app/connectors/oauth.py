"""Provider-agnostic OAuth machinery for managed connectors (ADR-0019 §1, #452).

The framework side of connector OAuth — everything that is NOT provider-specific
lives here, so a second managed connector implements only ``oauth_spec()`` (and
optionally ``fetch_account_email``) and inherits the whole flow:

* **PKCE** (S256, RFC 7636) — verifier + challenge generation.
* **The state store** — ``state`` is an **opaque, high-entropy, single-use
  handle**: a 256-bit random key into a server-side Redis record holding the
  flow bindings (tenant/user/source/verifier/generation). Nothing but the
  handle ever transits the browser — a signed-token design was rejected in
  ADR-0019 §1 because signing gives integrity, not confidentiality, and the
  PKCE verifier must never leave the server. ``consume`` is atomic (``GETDEL``):
  a replayed or concurrent callback finds nothing and is rejected.
* **Code exchange + refresh grant** over an injected ``httpx`` client —
  https-only token endpoints, bounded timeouts, typed vendor-free errors.

Token material handling: access/refresh tokens exist here only as function
return values on their way to the CC-C vault (refresh) or a request-scoped
client (access). They are never logged, never serialised into a source row, and
never appear in an exception message (errors carry stable ``code``\\ s only).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID

import httpx

__all__ = [
    "REAUTHORIZE_REQUIRED",
    "EgressNotAllowedError",
    "InMemoryOAuthStateStore",
    "OAuthExchangeError",
    "OAuthFlowRecord",
    "OAuthGrantDeadError",
    "OAuthSpec",
    "OAuthStateStore",
    "RedisOAuthStateStore",
    "TokenResponse",
    "build_authenticated_client",
    "build_authorization_url",
    "exchange_code",
    "generate_pkce",
    "refresh_access_token",
]

# Sentinel ``sources.last_error`` value marking a dead OAuth grant: the sync
# task writes it when the refresh grant fails closed, and the wire serializer
# maps it to ``reauthorize_required: true`` (ADR-0019 §1). One definition, both
# sides import it.
REAUTHORIZE_REQUIRED = "reauthorize_required"

# The state handle's entropy: 32 urlsafe bytes = 256 bits — unguessable, and the
# record TTL bounds any offline attack window besides.
_STATE_BYTES = 32
# PKCE verifier: 64 urlsafe-random bytes → an 86-char verifier (RFC 7636 allows
# 43–128; S256 challenge derived below).
_VERIFIER_BYTES = 64
# Bounded token-endpoint I/O: the exchange happens in the interactive callback
# request, so it must fail fast rather than hang a browser redirect.
_TOKEN_TIMEOUT_SECONDS = 15.0


class OAuthExchangeError(Exception):
    """The token endpoint rejected or failed the exchange/refresh (no token).

    ``code`` is a stable machine-readable discriminator (``provider_error``,
    ``invalid_response``); the message never carries token material.
    """

    def __init__(self, detail: str, *, code: str = "provider_error") -> None:
        self.detail = detail
        self.code = code
        super().__init__(detail)


class OAuthGrantDeadError(OAuthExchangeError):
    """The refresh grant is dead (``invalid_grant`` — revoked/expired).

    The caller fails the sync closed and surfaces *reauthorize required*
    (ADR-0019 §1); re-running the connect flow is the repair.
    """

    def __init__(self) -> None:
        super().__init__("refresh grant is dead", code="invalid_grant")


@dataclass(frozen=True, slots=True)
class OAuthSpec:
    """A connector's OAuth endpoints + client registration (ADR-0019 §4).

    Returned by a connector's ``oauth_spec()`` capability. ``client_secret`` is
    deployment-level config (read by the connector from ``core/config``); it
    rides this value object only from the connector into the exchange call and
    is never persisted or logged. ``extra_authorize_params`` carries provider
    quirks (e.g. Google's ``access_type=offline&prompt=consent``).
    ``allowed_hosts`` is the connector's **fixed pinned host set** (ADR-0019
    §5): the only hosts an authenticated run/probe client will dial — the
    framework's guard rejects anything else *before* the Authorization header
    could leave toward it.
    """

    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    allowed_hosts: tuple[str, ...] = ()
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


class EgressNotAllowedError(Exception):
    """An authenticated connector request was refused by the run guard.

    Raised by the framework's guard transport — off-allowlist host, non-https
    scheme, or a host resolving into a blocked range — **before** any bytes
    (or the injected auth header) leave toward the target. An off-guard request
    is a defect at the ADR-0009 §3 bar, never a silent dial.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class _AuthenticatedPinnedTransport(httpx.AsyncBaseTransport):
    """The credential boundary of a connector run (ADR-0019 §4).

    The bearer token lives **inside this transport** — it is injected into the
    outgoing request only after every check passes, so connector code holding
    the client can neither read the credential (``client.headers`` carries no
    auth) nor route it anywhere unchecked. Per request (httpx re-invokes the
    transport per followed hop, so any redirect is re-checked too):

    * **https-only** — credentials never transit cleartext, even to a pinned
      host (loopback excepted for local test doubles is deliberately NOT
      offered here — the pinned sets are real provider hosts);
    * **pinned host set** — the connector's fixed ``allowed_hosts`` (ADR-0019
      §5); anything else is refused;
    * **resolve + range-check + IP-pin** — the shared :mod:`app.net.egress`
      predicate (one SSRF definition): resolve-all/reject-any, then the socket
      connects to the validated IP with the original host preserved for
      routing (Host header) and TLS (SNI), closing the DNS-rebind window —
      the same mechanics as the MCP guard (ADR-0012 §4).
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        allowed_hosts: frozenset[str],
        access_token: str,
    ) -> None:
        self._inner = inner
        self._allowed = allowed_hosts
        self._access_token = access_token

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from app.net.egress import EgressBlockedError, pin_url_to_ip, resolve_safe_ip

        url = str(request.url)
        if request.url.scheme.lower() != "https":
            raise EgressNotAllowedError(
                f"scheme {request.url.scheme!r} refused: an authenticated connector "
                "request must be https"
            )
        host = (request.url.host or "").casefold()
        if host not in self._allowed:
            raise EgressNotAllowedError(f"host {host!r} is not in the connector's pinned allowlist")
        try:
            safe_ip = resolve_safe_ip(host)
        except EgressBlockedError as exc:
            raise EgressNotAllowedError(str(exc)) from exc

        pinned = pin_url_to_ip(url, safe_ip)
        pinned_request = httpx.Request(
            method=request.method,
            url=pinned,
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        pinned_request.headers["Host"] = request.url.netloc.decode("ascii")
        # The credential is attached HERE — after every check, never before.
        pinned_request.headers["Authorization"] = f"Bearer {self._access_token}"
        return await self._inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_authenticated_client(
    spec: OAuthSpec,
    *,
    access_token: str,
    timeout: float,
    inner_transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The framework-owned authenticated, guarded client (ADR-0019 §4).

    The ONLY constructor of a credential-bearing connector client. The bearer
    is held privately by :class:`_AuthenticatedPinnedTransport` and injected
    per hop after the https / pinned-host / resolve-range checks — the client
    object itself carries **no** auth header, so connector code (which receives
    this client via ``ConnectorRun`` or the identity probe, never a token
    string) cannot read the credential off it. Redirects are NOT auto-followed;
    a connector following one re-enters the guard. ``inner_transport`` is the
    test seam (a MockTransport behind the REAL guard); production uses the
    default transport.
    """
    allowed = frozenset(h.casefold() for h in spec.allowed_hosts)
    inner = inner_transport if inner_transport is not None else httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        transport=_AuthenticatedPinnedTransport(
            inner, allowed_hosts=allowed, access_token=access_token
        ),
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """The vendor-free projection of a token-endpoint response."""

    access_token: str
    refresh_token: str | None
    scope: str | None
    expires_in: int | None


@dataclass(frozen=True, slots=True)
class OAuthFlowRecord:
    """The server-side flow bindings an opaque ``state`` handle points at.

    Everything the callback needs to re-authorize **from current state** and
    complete the exchange — none of it ever transits the browser. ``generation``
    is compared against the source's current ``connect_generation`` so starting
    flow N+1 invalidates flow N even inside its TTL (ADR-0019 §1).
    """

    tenant_id: UUID
    user_id: UUID
    source_id: UUID
    source_type: str
    code_verifier: str
    redirect_uri: str
    generation: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_id": str(self.tenant_id),
                "user_id": str(self.user_id),
                "source_id": str(self.source_id),
                "source_type": self.source_type,
                "code_verifier": self.code_verifier,
                "redirect_uri": self.redirect_uri,
                "generation": self.generation,
            }
        )

    @staticmethod
    def from_json(raw: str) -> OAuthFlowRecord | None:
        """Parse a stored record; ``None`` for anything malformed (fail closed)."""
        try:
            data = json.loads(raw)
            return OAuthFlowRecord(
                tenant_id=UUID(data["tenant_id"]),
                user_id=UUID(data["user_id"]),
                source_id=UUID(data["source_id"]),
                source_type=str(data["source_type"]),
                code_verifier=str(data["code_verifier"]),
                redirect_uri=str(data["redirect_uri"]),
                generation=int(data["generation"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class OAuthStateStore(Protocol):
    """Single-use server-side state records (issue → consume-exactly-once)."""

    async def issue(self, record: OAuthFlowRecord, *, ttl_seconds: int) -> str:
        """Store ``record`` under a fresh opaque handle; return the handle."""
        ...

    async def consume(self, handle: str) -> OAuthFlowRecord | None:
        """Atomically fetch-and-delete the record, or ``None`` (unknown/expired/replayed)."""
        ...


def _new_handle() -> str:
    return pysecrets.token_urlsafe(_STATE_BYTES)


class RedisOAuthStateStore:
    """The production :class:`OAuthStateStore` over ``redis.asyncio``.

    ``consume`` uses ``GETDEL`` — the atomic single-use guarantee: exactly one
    of two concurrent callbacks can obtain the record; the other sees ``None``
    and is rejected (ADR-0019 §1).
    """

    _PREFIX = "connector-oauth:state:"

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            redis_url, decode_responses=True
        )

    async def issue(self, record: OAuthFlowRecord, *, ttl_seconds: int) -> str:
        handle = _new_handle()
        await self._redis.set(self._PREFIX + handle, record.to_json(), ex=ttl_seconds)
        return handle

    async def consume(self, handle: str) -> OAuthFlowRecord | None:
        raw = await self._redis.getdel(self._PREFIX + handle)
        if raw is None:
            return None
        return OAuthFlowRecord.from_json(raw)


class InMemoryOAuthStateStore:
    """Offline test double — same contract, no Redis (no TTL expiry simulation)."""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    async def issue(self, record: OAuthFlowRecord, *, ttl_seconds: int) -> str:
        handle = _new_handle()
        self._records[handle] = record.to_json()
        return handle

    async def consume(self, handle: str) -> OAuthFlowRecord | None:
        raw = self._records.pop(handle, None)
        if raw is None:
            return None
        return OAuthFlowRecord.from_json(raw)


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 S256."""
    verifier = pysecrets.token_urlsafe(_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _is_local_host(url: str) -> bool:
    """Loopback hosts get the explicit local-development http exception."""
    host = (httpx.URL(url).host or "").casefold()
    return host in {"localhost", "127.0.0.1", "::1"}


def build_authorization_url(
    spec: OAuthSpec,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The provider consent URL for the browser (authorization-code + PKCE).

    Carries the client id, scopes, the opaque ``state`` handle, and the S256
    challenge — never the verifier, never a secret. Both the authorize endpoint
    and the redirect URI must be **https** (state/code must not transit
    cleartext), with the explicit loopback exception for local development.
    """
    for url, what in ((spec.authorize_url, "authorize endpoint"), (redirect_uri, "redirect URI")):
        if not url.startswith("https://") and not _is_local_host(url):
            raise OAuthExchangeError(
                f"{what} must be https (state/code never transit cleartext)",
                code="insecure_endpoint",
            )
    params = {
        "response_type": "code",
        "client_id": spec.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(spec.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **spec.extra_authorize_params,
    }
    separator = "&" if "?" in spec.authorize_url else "?"
    return f"{spec.authorize_url}{separator}{urlencode(params)}"


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise OAuthExchangeError("token endpoint must be https", code="insecure_token_endpoint")


def _parse_token_response(resp: httpx.Response) -> TokenResponse:
    """Map a token-endpoint response to the vendor-free shape (fail closed).

    A non-2xx with ``error=invalid_grant`` is the dead-grant signal
    (:class:`OAuthGrantDeadError`); any other failure or malformed body is a
    generic :class:`OAuthExchangeError` — no token, no secret written.
    """
    body: dict[str, object]
    try:
        parsed = resp.json()
        body = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        body = {}
    if resp.status_code != 200:
        if body.get("error") == "invalid_grant":
            raise OAuthGrantDeadError()
        raise OAuthExchangeError(
            f"token endpoint returned {resp.status_code}", code="provider_error"
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthExchangeError("no access_token in response", code="invalid_response")
    refresh_token = body.get("refresh_token")
    scope = body.get("scope")
    expires_in = body.get("expires_in")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        scope=scope if isinstance(scope, str) else None,
        expires_in=expires_in if isinstance(expires_in, int) else None,
    )


async def exchange_code(
    http: httpx.AsyncClient,
    spec: OAuthSpec,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> TokenResponse:
    """Exchange an authorization code (server-side, PKCE verifier attached)."""
    _require_https(spec.token_url)
    try:
        resp = await http.post(
            spec.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": spec.client_id,
                "client_secret": spec.client_secret,
                "code_verifier": code_verifier,
            },
            timeout=_TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise OAuthExchangeError(
            f"token endpoint unreachable: {type(exc).__name__}", code="provider_error"
        ) from exc
    return _parse_token_response(resp)


async def refresh_access_token(
    http: httpx.AsyncClient,
    spec: OAuthSpec,
    *,
    refresh_token: str,
) -> TokenResponse:
    """Run the refresh grant; raises :class:`OAuthGrantDeadError` on ``invalid_grant``."""
    _require_https(spec.token_url)
    try:
        resp = await http.post(
            spec.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": spec.client_id,
                "client_secret": spec.client_secret,
            },
            timeout=_TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise OAuthExchangeError(
            f"token endpoint unreachable: {type(exc).__name__}", code="provider_error"
        ) from exc
    return _parse_token_response(resp)
