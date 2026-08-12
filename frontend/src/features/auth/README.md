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
  refresh token is never visible to JS. Only its non-secret UUIDv4 auth-slot
  selector is persisted so bootstrap can address the correct cookie after a
  reload.
- [`api/auth.ts`](../../api/auth.ts) — typed `login` / `refresh` / `getCurrentUser`
  / `logout` and `installAuthRefresh()` (wires the silent-refresh handler into the
  client). `login`/`refresh` use `skipAuth` so they carry no stale bearer and never
  recurse into the refresh loop. Every login reserves latest-intent authority
  before its first await and uses a distinct cookie slot.
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
  auth request coordinator qualifies refresh completion/retry with monotonic
  principal and auth-intent generations, so an old refresh cannot write a token
  or retry an A operation after logout or B login. Production mutations use the
  principal-scoped mutation wrapper, which rejects late results and suppresses
  old-generation callbacks (including paused/offline work).
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

The single-flight refresh coordinator owns its `AbortController`, and bootstrap,
401 retry, login, logout, and cross-document selector events all reserve the
same generation-checked transition path before the first bearer exists. A
selector event therefore aborts a pre-token login/bootstrap as well as an
authenticated request; no late A success or failure can commit after B.
Browser cancellation remains transport cleanup only: it cannot retract response
headers the browser already accepted. Cookie safety therefore lives at the wire
and server boundary. Each login's strict UUIDv4 slot is both its unique cookie
suffix and the stable `refresh_tokens.id`; only its canonical lowercase,
hyphenated wire spelling is accepted. Refresh locks by row ID before verifying
the token hash, so a blocked same-slot loser receives `refresh_superseded` without
revoking the winner or deleting its cookie. Tabs use Web Locks when available and
a non-secret completion revision for a quick retry; both are optional and the
server remains correct for lockless/crashed/non-SPA clients. Bearer-authenticated
logout revokes the tenant/user-bound row even if it captured a pre-rotation
cookie. A late logout expires only its own cookie name, never a later login's.
Legacy fixed-cookie logout revokes server-side without a shared `Delete-Cookie`
header.

The server serializes slot admission per tenant/user and bounds active families
with `AUTH_SESSION_MAX_ACTIVE` (default 8, validated 2–16). It protects the new
and currently selected active slots, revokes expired/excess families oldest-first
with a UUID tie-break, and returns deletion headers only for exact stale slot
names owned by that resolved user. This keeps the normal HttpOnly namespace and
request header comfortably bounded without making cookies visible to JS.

Superseded/cancelled successful logins revoke their own slot, a successful switch
retires the outgoing slot, and cross-tab selector changes revoke the old tab's
family while preserving the newer selector. If both an accepted login response
and its slot-specific cleanup remain unreachable, the inactive unique session is
never selected and is bounded by the configured refresh-token TTL; no client-only
mechanism can safely revoke an unknown accepted HttpOnly credential during that
network partition.

Browsers and extensions may ignore standards-correct hints or retain values in
their own vaults. The application cannot clear that third-party storage; precise
field semantics, short draft lifetimes, and DOM blanking are the enforceable
boundary here.

## Wiring it up at the wire-up with the live BE (#19)

This slice is contract-true today against mocks. At BE integration, confirm the
refresh cookie is set on login, that `POST /auth/refresh` mints from it, and that
the WS endpoint accepts `?access_token=…`.
