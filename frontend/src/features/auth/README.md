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
  `unauthenticated`) in Zustand. `model/PrincipalLifecycle.tsx` observes the token
  holder and cancels queries, wipes credentials, and clears both query/mutation
  caches before updating status on login, failed refresh, and logout. The user
  _object_ is server state and lives in TanStack Query, not here.
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

## Credential fields and local lifecycle (#580)

Credential inputs use explicit browser semantics:

- Login email uses `type="email"`, `name="email"`, and `autocomplete="username"`;
  the login password uses `name="password"` and `autocomplete="current-password"`.
- Provider/MCP credentials use domain-specific names and `SecretInput` with
  `autocomplete="new-password"`. Base URL and MCP endpoint use `type="url"`,
  URL input mode, no spellcheck, and no automatic capitalization.
- Reveal is an accessible, non-submit button with an announced pressed state.
  Every reset blanks the actual input and restores password masking, including
  manager-style DOM writes that never dispatched a React input event.
- Credential drafts start blank and stay in component state/ephemeral request
  holders. TanStack MutationCache receives only an opaque submission number;
  passwords, provider keys, and MCP tokens never enter persisted stores, browser
  storage, URLs, logs, or errors. Read responses contain only masked hints and
  never seed an input with an existing key.
- Forms clear on submission success/failure, cancel where offered, unmount,
  identity change, and logout; detached controls are blanked too. Untouched
  optional secrets are omitted from requests.

Logout clears the local bearer, credential holders, and both caches synchronously
before best-effort server revocation with the captured outgoing bearer. A
subsequent login in the same tab loads fresh account data. Ordinary refresh keeps
the same account's cache. Queued credential variables are discarded on a local
boundary and active credential requests receive an abort signal. Abort is
transport cleanup, not rollback of work already accepted and audited by the
server.

Standards-correct hints and `autocomplete="off"` cannot force a nonstandard
password manager to reclassify fields or erase its private vault. On shared or
managed browsers, use separate browser profiles and the manager's site exclusions
where needed. Application cleanup covers its own state and retained DOM nodes;
an extension can still inject values again after a reset.

This regression covers sequential A → logout → B in one tab/browser context.
Concurrent multi-tab/multi-principal session isolation is tracked separately in
[#646](https://github.com/k-sandhu/lumen-copilot/issues/646) /
[#647](https://github.com/k-sandhu/lumen-copilot/pull/647).

## Wiring it up at the wire-up with the live BE (#19)

This slice is contract-true today against mocks. At BE integration, confirm the
refresh cookie is set on login, that `POST /auth/refresh` mints from it, and that
the WS endpoint accepts `?access_token=…`.
