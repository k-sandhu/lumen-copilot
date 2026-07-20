# Guide — building a connector

> **Who this is for.** You are adding a new external source to Lumen Copilot.
> **What you write:** one package under `backend/app/connectors/<name>/` plus a
> conformance harness. **What you do not write:** OAuth, token storage, refresh,
> DB writes, transactions, cursor persistence, ACL enforcement, or a registry
> entry — the framework owns all of it.
>
> **Source of truth:** [ADR-0019](../architecture/0019-connector-sdk-and-oauth.md)
> (SDK, OAuth, ACL mirroring) and
> [ADR-0009](../architecture/0009-connector-framework-and-web-source.md)
> (the base framework + the SSRF/egress discipline). Where this guide and an ADR
> disagree, the ADR wins and this page is the bug.
>
> **Enforced by:** `backend/tests/test_connector_conformance.py` + the rules in
> `backend/tests/conformance/`. Every rule below is a test that fails with a
> message telling you what to change — the guide is the prose half of a
> mechanism, not advice.

---

## 1. The shape of a connector

A connector is a **drop-in package**. There is no registry to edit
(ADR-0008 §3): `app/connectors/registry.py` scans `app.connectors` for
subpackages exposing a module-level `CONNECTOR` and keys them by `name`.

```
backend/app/connectors/acme/
├── __init__.py      # CONNECTOR = AcmeConnector()   ← the whole registration
├── connector.py     # the protocol implementation
├── api.py           # thin HTTP helpers for the vendor's REST API
└── acl.py           # the pure ACL mapper (only if you mirror ACLs)
```

```python
# app/connectors/acme/__init__.py
from app.connectors.acme.connector import AcmeConnector

CONNECTOR = AcmeConnector()

__all__ = ["CONNECTOR", "AcmeConnector"]
```

`name` is the connector key: it is the `SourceType` value on the wire and the
`sources.type` column. Adding a new type also means adding it to the
`contracts/` enum — the FE/BE wire is contract-first (ADR-0006).

Two connectors exist today and are the worked examples: **`web`** (no
credentials, no capabilities — the minimum) and **`gdrive`** (all three
capabilities — the maximum).

## 2. The base protocol (mandatory)

`app/connectors/base.py::Connector`. Three operations plus a name:

```python
class AcmeConnector:
    name = "acme"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]: ...
    async def sync(self, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]: ...
    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth: ...
```

| Method | Runs where | Contract |
|---|---|---|
| `validate_config` | **request path**, before a row is written | Validate + normalise the user-supplied config; return the JSON to persist in `sources.config`. Raise `ConnectorConfigError` for anything invalid → the API answers **422** with your `code`. Synchronous and non-blocking: **no DNS, no network** (the `web` connector defers resolution to the sync path precisely for this reason). |
| `sync` | Celery task only | Full enumeration → `FetchedDoc`s. Raise `ConnectorError` on a fetch fault. |
| `health` | health probe | A cheap reachability/validity check. Return `ConnectorHealth(healthy=…, detail=…)` — **report** a fault, don't raise it. |

`sync` may return a plain list (that is all `web` does) or a `FullSyncResult`,
which is `Iterable[FetchedDoc]` and additionally carries the `baseline_cursor`
(the change-log start token, captured **before** enumeration begins) and
`skipped_count`. Returning `FullSyncResult` is how a capability-declaring
connector hands the framework its first cursor without breaking a base-protocol
caller.

### The domain values you return

Everything crossing the boundary is a domain type (ADR-0004) — never an `httpx`
response, never a vendor SDK object:

- **`FetchedDoc`** — `title`, `text`, `url`, plus the additive fields
  `external_id` (the provider's stable id, which turns reconcile into an upsert
  by `(source_id, external_id)` instead of delete-all), `modified_at`, `acl`
  (a `SourceAcl`), and `data`/`mime_type` for binary payloads the ingestion
  pipeline already parses (PDF/DOCX/…).
- **`SourceAcl`** — `{principals, scope_ids}`: the mirrored allow-list and the
  document's **container scope chain** (drive id + ancestor folder ids), so a
  container-permission change can stale-stamp descendants.
- **`SyncPage`** — one replayed change page (see §4).
- **`ConnectorHealth`** — the probe result.

## 3. The execution context — why you never see a credential or the DB

The framework constructs a **`ConnectorRun`** per run and passes it to `sync`,
`fetch_changes`, and `health`:

```python
@dataclass(frozen=True, slots=True)
class ConnectorRun:
    http: httpx.AsyncClient                  # already authenticated + egress-guarded
    acl_context: AclMappingContext | None    # the frozen identity snapshot
```

For an OAuth connector, by the time your code runs the framework has already
resolved `sources.auth_secret_ref` through the CC-C vault, run the refresh
grant, and built a guarded client. **The bearer lives inside the client's
transport, not on the client** — `run.http.headers` carries no `Authorization`
— and it is injected per hop only *after* the https / pinned-host / resolve-and-
range checks pass. So there is no token for connector code to read, log, or
misroute. You just make requests.

`map_acl` gets an **`AclMappingContext`**: a frozen dataclass holding a
case-folded `email → user_id` map of **attested** tenant users, the tenant
principal vocabulary, and the evaluation instant. Everything the mapping is
allowed to know is already in that snapshot.

**The prohibitions (ADR-0019 §4).** Connector code never reads:

| Forbidden | Because |
|---|---|
| the secrets vault (`app.services.secrets_service`) | the framework resolves credentials and hands you an authenticated client; a raw token never enters connector code |
| **Lumen's** database — `app.db` (session, models, repositories) **and its `database_url` setting** | persistence — and its transaction boundaries — belong to the framework; you return state as `FetchedDoc`/`SyncPage` fields and it commits them atomically |
| **Lumen's** object store — `app.storage` and the `s3_*` settings | same reason: return bytes on `FetchedDoc.data` |
| **Lumen's** other infrastructure + keys — `redis_url`, `celery_broker_url`, `celery_result_backend`, `jwt_secret`, `secrets_encryption_key` | none of it is yours to reach; the vault master key in particular is the secrets prohibition through another door |
| **mutable module-level state** | a connector must be re-entrant and hold nothing between runs; a module-level cache silently outlives the run and the tenant that filled it |

Note that the rule is stated in **two** halves, because banning the import alone
does not hold:

```python
from sqlalchemy.ext.asyncio import create_async_engine
create_async_engine(get_settings().database_url)   # imports nothing forbidden
```

That opens a connection straight into Lumen's database without touching
`app.db`. The *setting* is what distinguishes "Lumen's datastore" from "your
source", so the setting is pinned too.

Which is exactly what makes the following **allowed**:

- **`app.core.config` itself** — reading deployment-level, non-secret settings
  is what `oauth_spec()` does for the platform's client registration
  (`gdrive_oauth_client_id` and friends are fine).
- **A SQL client, `sqlalchemy` included, when your *external source* is a
  database.** Talking to a warehouse over SQL is a vendor boundary like any
  other (ADR-0004). Point it at a URL from **your own** `sources.config`; the
  prohibition is Lumen's database, not the existence of SQL.

These are pinned structurally by an AST scan of your whole package
(`tests/conformance/prohibitions.py`), covering imports that are deferred inside
a function body **and** relative ones (`from ...db import repositories`) — both
of the shapes a smuggled import actually takes. A module-level constant table
(`_EXPORT_MIME = {...}`, `__all__ = [...]`) is fine: the scan flags *mutation*
(`global`, a mutating method call, an item/attribute write) and mutable
containers bound to non-constant names (`_cache = {}`), with genuinely lexical
scoping — a local of the same name inside a nested helper does not excuse a
mutation in the enclosing function, and **a class attribute does not excuse one
in a method** (`CACHE.add(...)` inside a method resolves past `class C: CACHE =
set()` to the module global, and is a real violation).

**What the scan does not catch** — still on you and your reviewer: a dynamic
import (`importlib.import_module`, `__import__`), dynamic attribute access
(`getattr(settings, "database_url")`), state kept on a connector *instance*
attribute rather than a module global, and mutation reached through an alias.
The scan closes the accident-shaped holes; it is not a sandbox.

**Trust model.** v1 connectors are first-party, in-repo, code-reviewed Python
running **in-process** — the same trust boundary as the rest of the backend, so
no [ADR-0013](../architecture/0013-code-execution-sandbox.md) sandbox is
required. That holds *only* while every connector is first-party. Third-party /
SDK-only / push connectors are out of scope today, and isolation must be
revisited **before** any foreign code loads (ADR-0019 §4). Do not add an
in-process third-party connector on the strength of this guide.

## 4. Optional capabilities

You opt in by **defining the method**. There is no declaration list and no
registry flag — the framework duck-types the object
(`get_oauth_spec` / `get_fetch_changes` / `get_map_acl` in `base.py`).

### 4.1 `oauth_spec()` — managed authentication

```python
def oauth_spec(self) -> OAuthSpec: ...
```

Presence makes the source type **managed**: the framework drives the whole
authorization-code + PKCE flow, admin-gates every mutation (create, connect,
reconnect, sync-now, disconnect — checked against *current* roles, INV-5),
stores the refresh token in the CC-C vault, and refreshes it per run. You return
endpoints and registration only:

```python
OAuthSpec(
    authorize_url="https://accounts.example.com/o/oauth2/v2/auth",  # https
    token_url="https://oauth2.example.com/token",                   # https
    scopes=("https://www.example.com/auth/files.readonly",),        # non-empty
    client_id=settings.acme_oauth_client_id,
    client_secret=settings.acme_oauth_client_secret,
    allowed_hosts=("accounts.example.com", "oauth2.example.com", "api.example.com"),
    extra_authorize_params={"access_type": "offline", "prompt": "consent"},
)
```

Conformance requires: both endpoints **https**, a non-empty `scopes`, a
non-empty `allowed_hosts` of bare lowercase hostnames, and that the endpoints
you declare are inside your own pinned set.

Optionally implement `fetch_account_email(http)` — the post-exchange identity
probe. A read-only content scope typically returns **no identity claim** with
its tokens, so the framework calls this over the guarded client right after the
exchange to learn (and audit) the connected account.

### 4.2 `fetch_changes()` — incremental sync

```python
async def fetch_changes(
    self, source: Source, cursor: str, run: ConnectorRun
) -> AsyncIterator[SyncPage]: ...
```

Presence enables cursor-based replay (ADR-0019 §3). Yield `SyncPage`s:

```python
SyncPage(
    upserts=(...),                      # tuple[FetchedDoc, ...]
    deleted_external_ids=frozenset(),   # reconcile by identity
    next_cursor="…",                    # non-empty, always
    stale_scope_ids=frozenset(),        # container-cascade signal
    integrity=PageIntegrity.COMPLETE,   # or INCOMPLETE → fail closed source-wide
)
```

The contract, and why each part exists:

- **The framework commits one page per transaction** — the page's document
  mutations, its `stale_scope_ids` stamp, and `sources.sync_cursor =
  next_cursor` all together. A crash between pages resumes from the last
  committed token: never a skipped page, never half-applied state. That is why
  `next_cursor` must never be empty.
- **The terminal page's `next_cursor` is the new baseline** (the provider's
  "start from here next time" token). A cursor you emit must be a cursor you
  accept back — conformance feeds your terminal cursor straight back in.
- **Cascade signals are first-class fields** because you cannot touch the DB. A
  replayed permission change on a *container* usually emits no per-descendant
  event, so you put the container's id in `stale_scope_ids` and the framework
  stale-stamps every known descendant (matched via each document's
  `acl.scope_ids`) in the same transaction — immediate deny, not deny-at-window-
  expiry.
- **`integrity=INCOMPLETE` is the fail-closed escape hatch.** If you cannot
  prove the affected set complete (missing metadata, enumeration failure, budget
  exhausted), say so and the framework stamps **every** mirrored document of the
  source stale. Under-serving is always correct; guessing is not.
- **An invalid or expired cursor raises `CursorExpiredError`** — the typed
  signal, and it must be raised **before** you yield any page for that token.
  The framework then clears `sources.sync_cursor` and falls back to a full
  resync in the same run. A generic `ConnectorError` fails the sync instead of
  self-healing; a silent replay half-commits a token the provider has disowned.
- **An *ordinary* fault is a plain `ConnectorError`, never `CursorExpiredError`.**
  A transient 500 or a dropped connection must leave the committed cursor alone
  so the next run resumes where this one stopped. Reaching for the expiry signal
  on any failure throws away a valid resume point and re-enumerates the whole
  corpus every time the provider hiccups. Conformance checks both directions.

Full `sync()` stays mandatory: it is the bootstrap and the fallback. A null
`sync_cursor` means the next sync is full.

> **Conformance will ask you to prove the cascade signals actually fire**, not
> just that the fields type-check: your harness supplies a replay containing a
> container-permission change (⇒ non-empty `stale_scope_ids`) and one whose
> effects cannot be proven (⇒ `integrity=INCOMPLETE`). If your source has no
> containers, declare neither and the rules skip.

### 4.3 `map_acl()` — ACL mirroring

```python
def map_acl(self, raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]: ...
```

Presence marks every document the connector produces `acl_enforced=true`
(ADR-0019 §2) — the mode is derived **structurally**, never defaulted. That is a
strong statement: for those documents the mirrored principal set is the **only**
enforcement leg. The owner leg and Lumen grants do not apply; an empty or stale
mirror admits nobody, **including the connecting admin**.

The mapping is:

- **Pure** — a plain (non-`async`) function, deterministic, no I/O, and it must
  mutate **neither** input: not the snapshot, not the raw payload. Everything it
  may know is in `ctx` — including the evaluation instant, which is why you read
  `ctx.evaluated_at` rather than the clock.
- **Fail-closed** — anything unmappable grants nobody. Unknown type, unknown
  role, malformed entry, empty input: `frozenset()`.
- **Never-escalating** — the mapped set is provably a **subset** of the source's
  own allow-list. Under-sharing is fine and expected; a single principal outside
  it is a privilege escalation through the mirror.

> **What the purity rule actually enforces.** The kit checks determinism across
> repeated calls, immutability of both `raw` and `ctx`, and blocks sockets and
> `open()`. It does **not** intercept the clock: a mapper that calls
> `datetime.now()` will pass and still be wrong (two documents in one sync can
> then disagree). Use `ctx.evaluated_at`; that one is review-caught, not
> test-caught.

Principal vocabulary (v1): `user:<lumen_user_uuid>` and `tenant`. Use
`ctx.principal_for_email(email)` — it only resolves **attested** identities
(`users.email_attested_at`); an unattested match maps to nothing and is counted
in `unmapped_acl_count` so an admin can see what attestation would light up.

**Pending prerequisite — the INV-2 negative-test kit (F-CB-3,
[#454](https://github.com/k-sandhu/lumen-copilot/issues/454)) has not landed
yet.** When it does, a `map_acl`-declaring connector must pass it as well: it is
the ACL-proof suite covering cross-tenant exclusion, owner-denied and
grantee-denied on empty/stale mirrors in **both** stores, stale-window denial,
revocation-after-sync, and the never-escalate property end to end. Until then
there is **no runnable INV-2 gate to point you at** — the closest existing
coverage is `backend/tests/test_acl_mode_split.py` and
`backend/tests/test_gdrive_acl_mapping.py`, and you should extend those with
your connector's fixtures. Note what this means today: the conformance kit
proves your mapper's *shape* (fail-closed, pure, never-escalating over the
fixtures you supply); nothing yet proves the *semantics* hold through retrieval
for a new connector. Treat an ACL-declaring connector as not-done until #454
lands and you are in it.

#### Current, deliberate limits (v1)

Do not "fix" these in a connector — they are decided under-sharing, and widening
them needs an identity/directory decision, not connector code:

- **domain shares (`type=domain`) map to nothing.** A tenant may contain guests
  outside the sharing domain and there is no verified workspace↔tenant domain
  binding, so the subset proof fails.
- **groups are not expanded** (`type=group` ⇒ deny) — Directory/SCIM territory.
- **unattested emails map to nothing**; SSO federation supersedes attestation
  when it lands.
- **any `expirationTime` on an entry denies it** — v1 does not model time-boxed
  shares (no `acl_expires_at`), so a mirror can never outlive a temporal grant.
- Drive's `my_drive` mode currently syncs a **wider** set than its label
  suggests (it includes shared-with-me items) — open, tracked in
  [#475](https://github.com/k-sandhu/lumen-copilot/issues/475). Don't assume a
  mode's label defines its corpus until that lands.

## 5. Errors — the taxonomy

One typed hierarchy, in `app/connectors/base.py`. **Never let a vendor or
builtin exception escape a protocol method.**

| Raise | When | The framework does |
|---|---|---|
| `ConnectorConfigError(detail, code=…)` | the user's config is invalid — permanent rejection of the input | API answers **422** carrying your `code` (INV-8) |
| `ConnectorError(detail, code=…)` | a sync/fetch fault | source → `status=error`, `last_error` recorded, audited as `source.synced` `outcome=error` |
| `CursorExpiredError()` | the provider rejected the cursor (e.g. HTTP 410) | clears `sync_cursor`, falls back to a full resync in the same run |

`code` is a **stable, machine-readable discriminator** (`invalid_config`,
`url_blocked`, `cursor_expired`, `drive_api_error`, …) that the API surfaces in
the Problem body — pick one per failure mode and don't churn it. `detail` is
safe prose: never a token, never a raw vendor error body.

**This applies past `validate_config`.** Conformance drives your connector
against a *failing* provider and requires that `sync()` and `fetch_changes()`
surface a `ConnectorError` — a leaked `httpx.HTTPError` or `ValueError` becomes
a 500 with vendor detail instead of a recorded, safe `last_error`. And
`health()` **reports** a fault (`ConnectorHealth(healthy=False, detail=…)`)
rather than raising it: a raising probe turns one unreachable source into a
failed request for the whole connector grid.

**"Stable" is checked, not assumed.** Each fault is driven **twice** and the
code must be identical both times *and* equal to the value your harness declares
(`sync_fault_code` / `changes_fault_code`). A per-occurrence code
(`f"drive_error_{uuid4()}"`) is non-empty and still useless — nobody can match
on it, search for it, or count it. Pin one code per failure mode; changing it
later is a visible diff in the harness, which is the point.

## 6. Egress — the SSRF obligations

The rule from [ADR-0009 §3](../architecture/0009-connector-framework-and-web-source.md),
unchanged: **a request leaving the pinned host set is a blocking defect**, not a
warning.

- **One SSRF definition:** `backend/app/net/egress.py` — blocked-range checks,
  resolve-all/reject-any, IP-pinning against DNS rebinding. Do not write your
  own; every consumer (`connectors/web/fetch.py`, the MCP guarded client, the
  LLM provider catalog, the connector run client) shares it.
- **Pin your hosts.** `oauth_spec().allowed_hosts` is a *fixed* set of real
  provider hostnames. The framework's transport enforces https + allowlist +
  resolve/range-check + IP-pin **before** the credential is attached, and it
  re-runs per hop, so a redirect is re-checked too. Redirects are not
  auto-followed.
- **No user-supplied URLs** in a managed connector. Every request target should
  be built from constants in your `api.py`. If your connector *must* take a URL
  from user config (the `web` connector does), it goes through the shared guard
  on every fetch, and every child URL is re-validated — a hostile feed pointing
  at `169.254.169.254` must not pivot the server.
- **Bound everything:** timeouts, streamed size caps (never buffer an unbounded
  body), bounded retries with backoff on the provider's throttle response, and a
  descriptive User-Agent.
- Connector syncs run **only** in the Celery task, never the request path.

## 7. Register, verify, ship

1. **Drop the package in.** `CONNECTOR = YourConnector()` in `__init__.py`. No
   registry edit; the scan finds it.
2. **Add the contract enum value** for the new `SourceType` in `contracts/`.
3. **Add a conformance harness** — `backend/tests/conformance/harnesses.py`: an
   offline `Source`, a `ConnectorRun` over an `httpx.MockTransport`, a
   **faulting** run plus the stable `sync_fault_code` / `changes_fault_code` it
   must report, the invalid configs your `validate_config` must reject, and one
   fixture per declared capability — a replay cursor, an expired cursor, a
   transient-fault cursor, and (if your source has containers) a cascade + an
   unprovable page; ACL cases carrying the source's own allow-list for the
   subset proof. **A connector package that does not enroll, or that enrolls
   without a harness, fails the suite** — that is the point.

   "Enrolls" is checked on the object, not the name: your package's own
   `CONNECTOR` must be conformant, carry `name` equal to its directory, and be
   the very object the registry resolved for that name. A package whose
   `CONNECTOR` is broken does not quietly disappear from the suite — which is
   what happened before, because registry discovery *skips* a non-conforming
   `CONNECTOR` rather than raising.
4. **Run the gates** from `backend/`:
   ```bash
   uv run --extra dev pytest tests/test_connector_conformance.py
   uv run --extra dev ruff check app tests
   uv run --extra dev mypy app
   ```
5. **If you declared `map_acl`**, extend today's ACL coverage
   (`tests/test_acl_mode_split.py`, `tests/test_gdrive_acl_mapping.py`) with your
   fixtures, and join the INV-2 kit once #454 lands (§4.3).
6. **Migrations**: `sources` already carries `auth_secret_ref`, `sync_cursor`,
   `connect_generation`, and `connected_account`; documents already carry
   `external_id`, `acl_enforced`, `acl_principals`, `acl_synced_at`, and
   `acl_scope_ids`. A new connector should need **no** schema change. If you
   think you do, that is an ADR conversation first.

## 8. Deployment prerequisites (operational, per deployment)

A managed connector is not "done" when the code merges — someone has to
register an OAuth app.

**Any OAuth provider:**

1. Register an OAuth **client** (web application) in the provider's developer
   console.
2. Add the redirect URI: `https://<your-host>/api/v1/sources/oauth/callback` —
   the one shared callback for every OAuth connector.
3. Supply the credentials as deployment config (`pydantic-settings`, never in
   code or compose).
4. **Add your own non-local fail-fast validator — there is no generic one.**
   The only startup blank-refusal that exists today is hard-coded to
   `GDRIVE_OAUTH_CLIENT_ID` / `GDRIVE_OAUTH_CLIENT_SECRET`
   (`Settings._require_gdrive_oauth_client_in_prod` in `app/core/config.py`),
   and conformance does not check registration fields. So a new managed
   connector that only adds settings will start **blank in production** and fail
   at first connect instead of at boot. Add the settings *and* a matching
   validator modelled on the Google one, with a test. (Generalising this into
   one mechanism — every declared `oauth_spec` connector's config refused blank
   outside `local` — is a worthwhile follow-up; it is not in place.)
5. Per-tenant bring-your-own-client is **not** supported in v1 — the client
   registration is platform-level (recorded follow-up).

**Google Drive specifically:**

- Create a Google Cloud project, enable the **Drive API**, configure the OAuth
  consent screen, and create an **OAuth client ID** (Web application).
- Set `GDRIVE_OAUTH_CLIENT_ID` / `GDRIVE_OAUTH_CLIENT_SECRET`.
- **`https://www.googleapis.com/auth/drive.readonly` is a Google *restricted*
  scope.** A production/public deployment therefore requires Google's
  **verification review** — including, for restricted scopes, a security
  assessment. Budget real calendar time for it. Local and dev runs use a
  test-mode client with the consent screen in *Testing* and the connecting
  accounts added as test users; no verification is needed there.
- The connector only ever dials `accounts.google.com`, `oauth2.googleapis.com`,
  and `www.googleapis.com` (§6).

Also operational, per deployment: the ACL **freshness window**
(`CONNECTOR_ACL_MAX_AGE_HOURS`, default 24) is the worst-case
revocation-to-enforcement bound — shorten it and stalled syncs hide content
sooner; lengthen it and a revoked share stays visible longer. And the sync poll
interval (`CONNECTOR_SYNC_INTERVAL_MINUTES`, default 60).

## 9. Where things live

| Concern | File |
|---|---|
| Protocol, domain types, capability getters | `backend/app/connectors/base.py` |
| Auto-discovery | `backend/app/connectors/registry.py` |
| OAuth machinery (PKCE, state store, exchange/refresh, guarded client) | `backend/app/connectors/oauth.py` |
| The shared SSRF/egress primitive | `backend/app/net/egress.py` |
| The framework's sync task (runs you, commits your pages) | `backend/app/tasks/sync_source.py` |
| Worked example — no capabilities | `backend/app/connectors/web/` |
| Worked example — all three capabilities | `backend/app/connectors/gdrive/` |
| Conformance rules + harnesses | `backend/tests/conformance/`, `backend/tests/test_connector_conformance.py` |
| ACL enforcement coverage that exists **today** | `backend/tests/test_acl_mode_split.py`, `backend/tests/test_gdrive_acl_mapping.py` |
| ACL/INV-2 negative-test kit | **not yet built** — F-CB-3, [#454](https://github.com/k-sandhu/lumen-copilot/issues/454) |
