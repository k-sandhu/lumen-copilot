# Auth — login, session & bearer wiring (`features/auth`)

The frontend auth slice (issue #48), built against the **frozen** `/auth` contract
(`contracts/openapi.yaml` 0.1.0) and spec 0004 §2.3 (app-managed identity:
short-lived access JWT + rotating httpOnly refresh cookie). It builds in parallel
with the backend (ADR-0006) — conform to the contract, mock the responses in dev
and tests.

## Where the pieces live

Transport (the `api/` boundary — the only backend caller):

- [`api/token.ts`](../../api/token.ts) — the access token holder. **In memory
  only**, never `localStorage`/`sessionStorage` (least-exposure, spec 0004): a
  reload drops it and the app silently refreshes from the httpOnly cookie. The
  refresh token is never visible to JS.
- [`api/auth.ts`](../../api/auth.ts) — typed `login` / `refresh` / `getCurrentUser`
  / `logout` and `installAuthRefresh()` (wires the silent-refresh handler into the
  client). `login`/`refresh` use `skipAuth` so they carry no stale bearer and never
  recurse into the refresh loop.
- [`api/client.ts`](../../api/client.ts) — every request gets `credentials:
'include'` (so the refresh cookie rides along) and, when a token is held,
  `Authorization: Bearer …`. On a **401** it performs **one** silent refresh via the
  registered handler, then retries the original request once; a failed refresh
  surfaces the 401 so the guard routes to login.
- [`api/ws.ts`](../../api/ws.ts) — the WebSocket handshake can't set headers, so the
  bearer token is appended as `?access_token=…` (resolved at connect time, so a
  refreshed token is used on reconnect).

Feature (`features/auth`):

- `model/authStore.ts` — coarse session status (`unknown` | `authenticated` |
  `unauthenticated`) in Zustand. The user _object_ is server state and lives in
  TanStack Query, not here.
- `model/PrincipalLifecycle.tsx` — binds token transitions to the exact
  `QueryClient` owned by the surrounding provider. Before a lost/replaced
  principal can render, it cancels queries, destroys/aborts credential holders,
  clears every query and mutation, and only then changes route status. A normal
  same-principal access-token refresh is not treated as an account switch. The
  auth request coordinator qualifies refresh completion/retry with a monotonic
  principal generation, so an old refresh cannot write a token or retry an A
  operation after logout or B login.
- `model/queries.ts` — `useCurrentUser` (`GET /auth/me`), `useLogin`, `useLogout`.
- `model/useBootstrapSession.ts` — one boot-time silent refresh so a reload keeps
  the session; until it resolves the guard shows a loading state (no login flash).
- `components/LoginScreen.tsx` — email+password → `POST /auth/login`. Bad creds show
  a **single generic** message (AC-4: no account-existence disclosure).
- `components/RouteGuard.tsx` — unauthenticated → login; authenticated → children;
  bootstrapping → loading.
- `components/CurrentUserMenu.tsx` — current user + sign out, in the shell header.

## Invariant honored

INV-4 (spec 0004): no access without a valid, unexpired token — a 401 triggers
refresh-then-retry, and a failed refresh ends in the login screen. The negative
paths (bad creds → generic error, expired token → refresh+retry, failed refresh →
login) are covered in `authClient.test.ts`, `auth.test.ts`, `LoginScreen.test.tsx`,
and `RouteGuard.test.tsx`.

## Credential-field and draft-lifecycle contract

Credential inputs use explicit browser semantics instead of treating
`autocomplete="off"` as a security boundary:

- the real login form is password-manager compatible: the email field is named
  `email` with `autocomplete="username"`, and the password field is named
  `password` with `autocomplete="current-password"`;
- non-login API keys and bearer tokens use domain-specific names and the shared
  `SecretInput` primitive with `autocomplete="new-password"`; adjacent endpoints
  are named URL inputs so they cannot be mistaken for usernames;
- raw drafts stay only in controlled component state and the ephemeral request
  holder. TanStack MutationCache receives an opaque token, not the request body,
  and local/session storage, query strings, logs, and read responses must never
  receive a raw credential;
- the owning form uses an explicit hard reset on cancel, settled submission,
  unmount, principal change, and logout. The reset blanks retained DOM property
  values (including manager writes that emitted no input event) and restores
  secret controls to `type=password` without scheduling state after unmount.
  Provider/server reads expose only presence or a masked fingerprint, so an
  existing secret is never hydrated back into the DOM.

At a principal boundary, every query is cancelled and both TanStack caches are
emptied, queued ephemeral mutations destroy their request variables before they
can start, and dispatched credential requests receive an abort signal. Explicit
logout performs that local teardown synchronously at click intent, while the
best-effort revocation request keeps the outgoing principal's captured bearer.
Its late result cannot re-authenticate or clear a later session. Abort is not a
server-side rollback: if the server already accepted a request, it remains
authorized and audited under the bearer attached at dispatch. It is never
reissued from the queued holder under the next principal.

The single-flight refresh coordinator owns its `AbortController`. Login/logout
invalidate the old generation, abort its refresh fetch, and await that JS promise
before issuing the boundary request; this also orders any old refresh-cookie
response the browser did accept before the later login/logout cookie response.
Likewise, a later login waits for the outgoing logout's cookie-clearing response.
Both barriers are bounded: a genuinely hung fetch is aborted after 1.5 seconds so
sign-in cannot remain wedged indefinitely. Browser cancellation is only
best-effort transport cleanup: if the server already rotated the refresh token or
response headers (including `Set-Cookie`) arrived, JavaScript cannot undo them.
When the old response settles within the barrier, the later boundary response is
deliberately last; after a timeout, server/header ordering is residual risk. The
generation gate still rejects stale access-token writes and original-request
retries, and this frontend does not claim server rollback.

Browsers and extensions may ignore standards-correct hints or retain values in
their own vaults. The application cannot clear that third-party storage; precise
field semantics, short draft lifetimes, and DOM blanking are the enforceable
boundary here.

## Wiring it up at the wire-up with the live BE (#19)

This slice is contract-true today against mocks. At BE integration, confirm the
refresh cookie is set on login, that `POST /auth/refresh` mints from it, and that
the WS endpoint accepts `?access_token=…`.
