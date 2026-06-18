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
  `unauthenticated`) in Zustand. Subscribes to the token holder so a cleared token
  (failed refresh / logout) deterministically routes back to login. The user
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

## Wiring it up at the wire-up with the live BE (#19)

This slice is contract-true today against mocks. At BE integration, confirm the
refresh cookie is set on login, that `POST /auth/refresh` mints from it, and that
the WS endpoint accepts `?access_token=…`.
