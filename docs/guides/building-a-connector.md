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
├── __init__.py      # CONNECTOR = AcmeConnector   ← the whole registration
├── connector.py     # the protocol implementation
├── api.py           # thin HTTP helpers for the vendor's REST API
└── acl.py           # the pure ACL mapper (only if you mirror ACLs)
```

```python
# app/connectors/acme/__init__.py
from app.connectors.acme.connector import AcmeConnector

CONNECTOR = AcmeConnector

__all__ = ("CONNECTOR", "AcmeConnector")
```

`CONNECTOR` retains the class declaration. Class methods preserve the framework's
call signatures without a shared instance; instantiate helpers inside a run.

`name` is the connector key: it is the `SourceType` value on the wire and the
`sources.type` column. Adding a new type also means adding it to the
`contracts/` enum — the FE/BE wire is contract-first (ADR-0006).

Two connectors exist today and are the worked examples: **`web`** (no
credentials, no capabilities — the minimum) and **`gdrive`** (all three
capabilities — the maximum).

## 2. The base protocol (mandatory)

`app/connectors/base.py::Connector`. Three operations plus a name:

```python
from dataclasses import dataclass

@dataclass(frozen=True, eq=False, repr=False)
class AcmeConnector:
    name = "acme"

    @classmethod
    def validate_config(cls, config: dict[str, object]) -> dict[str, object]: ...
    @classmethod
    async def sync(cls, source: Source, run: ConnectorRun) -> Iterable[FetchedDoc]: ...
    @classmethod
    async def health(cls, source: Source, run: ConnectorRun) -> ConnectorHealth: ...
```

| Method | Runs where | Contract |
|---|---|---|
| `validate_config` | **request path**, before a row is written | Validate + normalise the user-supplied config; return the JSON to persist in `sources.config`. Raise `ConnectorConfigError` for anything invalid → the API answers **422** with your `code`. Synchronous and non-blocking: **no DNS, no network** (the `web` connector defers resolution to the sync path precisely for this reason). |
| `sync` | Celery task only | Full enumeration → `FetchedDoc`s. Raise `ConnectorError` on a fetch fault. |
| `health` | health probe | A cheap reachability/validity check. Return `ConnectorHealth(healthy=…, detail=…)` — **report** a fault, don't raise it. |

The returned config must survive strict JSON serialization and read-back without
changing any nested value or key: string-keyed objects, lists, JSON scalars and
finite numbers. Convert vendor URL objects to strings; tuples, bytes, sets,
non-string nested keys and NaN/infinities fail conformance. The harness feeds
the normalized JSON read-back into the Source used by sync, health and replay.

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
| **Lumen's** database — `app.db` (session, models, repositories), and its `database_url` **via the settings object** | persistence — and its transaction boundaries — belong to the framework; you return state as `FetchedDoc`/`SyncPage` fields and it commits them atomically |
| **Lumen's** object store — `app.storage`, and the `s3_*` settings | same reason: return bytes on `FetchedDoc.data` |
| **Lumen's** other infrastructure + keys — Redis/Celery, OpenSearch endpoint/index/credentials, sandbox-runner endpoint, LLM-provider API key, JWT/vault keys | none of it is yours to reach; every field is explicitly classified in `tests/conformance/prohibitions.py` |
| **mutable module-level state** | a connector must be re-entrant and hold nothing between runs; a module-level cache silently outlives the run and the tenant that filled it |

**Fail-closed import allowlist (P1).** Absolute and relative static imports,
including imports inside functions, may reach only the Python standard library,
third-party packages, your own connector package, pure `app.domain` modules,
and these exact shared modules derived from the shipped `gdrive` and `web`
imports:

| Exact module | Reason |
|---|---|
| `app.connectors.base` | Both connectors use its protocols, framework-supplied run/ACL contexts, domain results and typed errors. |
| `app.connectors.oauth` | Drive returns `OAuthSpec` from `oauth_spec()`; credential resolution and state-store execution remain framework-owned. |
| `app.core.logging` | Drive obtains `get_logger` inside calls for operational logs. |
| `app.net.egress` | Web uses the shared SSRF address/DNS checks and IP-pinning helpers. |
| `app.core.config` — only unaliased `get_settings` | The read-only shape seal below permits sanctioned deployment-config reads. |

`ALLOWED_FRAMEWORK_IMPORTS` lists the four shared modules explicitly, with
reasons. It grants neither their parent packages nor additional submodules;
`from app.connectors import base` selects the same allowed leaf. Your own
package includes its submodules; another connector's package is not allowed.
All other `app` dependencies fail closed, including `app.api`, `app.services`,
`app.db`, `app.tasks`, `app.search` and `app.llm`. Adapters never depend on the
API/service layer (ADR-0004). In particular, `from app.api.deps import
get_settings_dep` or a renamed settings reexport cannot bypass the config seal.

The conformance suite independently audits all four shared modules and every
pure-domain module for unsafe imports, Settings/accessor reexports and exposed
infrastructure handles. Config itself is the intentional exception: only the
sanctioned accessor name and read shape are available. A new shared dependency
requires an explicit allowlist entry, a reason here and the same audit; an
unlisted helper wrapper is forbidden even if its current implementation looks
harmless. Third-party clients for an external source remain subject to the
existing Lumen-infrastructure prohibitions and first-party review.

Banning the import alone does not hold, because this reaches Lumen's database
without touching `app.db`:

```python
from sqlalchemy.ext.asyncio import create_async_engine
create_async_engine(get_settings().database_url)   # imports nothing forbidden
```

The seam that leaks here is the **settings object**, so that is what is sealed —
by *shape*, not by analysing the read. A connector may touch settings in exactly
one form:

```python
get_settings().<field>          # read ONE deployment-config field, directly
```

The accessor may not be aliased, stored, passed, or transformed, and the read
must stop at the field. So a connector never *holds* a settings object — and
with nothing to hold, there is nothing to launder: `create_async_engine(...)`,
`getattr(...)`, `.model_dump()[...]`, a conditional, a walrus, a hand-off to a
helper all fail the same way, for *binding the object at all*. Reading a Lumen
**infrastructure** field (`database_url`, `s3_secret_key`, `redis_url`,
`jwt_secret`, `secrets_encryption_key`, …) is refused even through the one legal
shape.

`<field>` must be listed in `ALLOWED_DEPLOYMENT_CONFIG`. Storing a bound method
such as `dump = get_settings().model_dump` also retains the settings object and
is refused, even if the method is called later. Only the unaliased accessor
import is allowed; relative imports are normalized to the same module name,
and importing the config module through its parent is refused too.

The field value may be used as an ordinary expression, including a dictionary
key (`by_agent[get_settings().web_user_agent]`). The ban on a trailing subscript
applies when indexing the field itself (`get_settings().web_user_agent[0]`).

This makes the external-SQL allowance hold **by construction**, not by a
second rule that has to keep pace:

```python
connector_config.database_url                  # legal — not get_settings-rooted
connector_config.model_dump()["database_url"]  # legal — your own typed config
source.config["database_url"]                  # legal — your own sources.config
```

The scan only ever inspects expressions rooted at `get_settings`, so a
connector's own config — whatever it is named, however it is read — is invisible
to it. That is deliberate and it is the load-bearing half: the failure direction
here is the opposite of a credential lint, because over-reporting breaks a
legitimate connector.

Which is why the following stay **allowed**:

- **Deployment config via `get_settings().<field>`** — this is how
  `oauth_spec()` reads the platform's OAuth client registration
  (`get_settings().gdrive_oauth_client_id`), and what `web` does for its
  User-Agent (ADR-0019 §4/§5). Read it directly; don't bind the object. Which
  fields count as "Lumen infrastructure" versus "deployment config" is not a
  judgement call left to the scan: every field on the `Settings` model is
  classified in `FORBIDDEN_SETTINGS` or `ALLOWED_DEPLOYMENT_CONFIG`, and a
  completeness test asserts that against the live model — so a new infra field
  added to `Settings` fails CI until it is consciously classified, rather than
  slipping through the legal read shape unlisted. The Google OAuth client
  secret is also allowed: ADR-0019 sanctions reading that connector's client
  registration in `oauth_spec()`; it is distinct from Lumen's infrastructure
  credentials and the per-source token held by the vault.
- **A SQL client, `sqlalchemy` included, when your *external source* is a
  database.** Talking to a warehouse over SQL is a vendor boundary like any
  other (ADR-0004). With the settings seam sealed, such a client can only ever
  reach the warehouse *you* configured; the prohibition is Lumen's database, not
  the existence of SQL.

These are pinned structurally by an AST scan of your whole package
(`tests/conformance/prohibitions.py`), including function-local and relative
imports (`from ...db import repositories`). Deployment config is **read-only**:
assignment, `+=`, and `del` on `get_settings().<field>` are violations even for
an allowed field. Keep per-source configuration in the run's own local values.

**Cross-call retention inventory (P3).** Ordinary Python retains values through
the following storage mechanisms; P3 checks each before a connector can enroll:

1. **Module bindings**, including aliases, unpacking and control-flow bindings.
   Tuples, frozensets and mapping proxies must match IMM below. All retained
   constructor calls fail closed, including frozen dataclasses and NamedTuples.
2. **Function/lambda defaults**, positional and keyword-only, at every nesting
   level. Only provably immutable defaults are allowed. Use `None` and create
   the container inside the call, including for nested helpers.
3. **Class attributes**, including plain assignments, dataclass `ClassVar`
   and attributes assigned after the class body. They must be immutable;
   per-instance `field(default_factory=...)` metadata is not a ClassVar.
4. **Function attributes**, including `f.cache = ...`, deletion, aliases and
   `setattr(f, ...)`. Attribute writes are forbidden even for immutable values.
5. **Persistent memoizers/descriptors**: `functools.lru_cache`, `cache`,
   `cached_property`, cachetools memoizers and custom decorators whose state
   cannot be proved call-local. P3 rejects them; there is no cache allowance.
6. **Import-time factory closures**, partials, bound methods, generators and
   other retained callable/iterator results. Unknown call results fail closed,
   so enclosing mutable captures cannot escape into a module binding or default.
7. **`global`/`nonlocal` rebinding**, including scalar state. Both are forbidden.
8. **Singleton instance fields**, including nested mutation through an alias.
   Singleton instances are forbidden. Stateless entry points retain the class
   declaration and use class methods, as the shipped connectors do. Existing
   instance callers remain supported; instances are created inside calls only.

Imports of code/modules are explicit
trust boundaries; unknown imported *payload values* are not immutable defaults
or container contents. Static imports within your connector package are scanned
with that package; shared modules are audited as described above. External
libraries remain subject to first-party review.
This inventory covers ordinary language storage, not a sandbox: dynamic
imports, computed reflection, `exec`/`eval`, `sys.modules` and frame tricks are
review-caught residuals. External file/service persistence is separately
restricted by P1/P2 and the framework-owned execution context.

**Owner rules (2026-09-30, verbatim).**

1. REMOVE the record-constructor grammar alternative entirely: no dataclass / NamedTuple / Enum-member *instances*
   (or any other class instance) may be bound at module scope, as class attributes, or as defaults. Retained values
   are limited to the remaining IMM forms (constants, literal tuple/frozenset/MappingProxyType displays of IMM,
   re.compile(constant), constant arithmetic, references to validated module names / imports). Enum and NamedTuple
   CLASS definitions themselves stay allowed; referencing an Enum member by name (e.g. `Kind.FILE`) as a value is
   allowed only if the scanner can see the Enum class is defined in the package with IMM member values and no
   metaclass= / __init_subclass__ customisation — otherwise forbid it.
2. Ban dunder reflection in connector packages as fail-closed violations: any reference to `__dict__`, `__setattr__`,
   `__delattr__`, `__class__`, `__slots__` mutation, `object.__setattr__`, `vars(...)`, `setattr(...)`/`delattr(...)`
   on non-local objects, and any `metaclass=` keyword or class defining `__init_subclass__` / `__set_name__` / `__prepare__`.

The reflection ban applies to static attributes, named references and constant
`getattr`/setter names, including built-in import aliases. `__slots__` attribute
access also fails closed to prevent borrowing it for mutation; an immutable
`__slots__` declaration in the class body stays legal. Use `dataclasses.asdict`
or an explicit fresh payload for a snapshot; live namespace borrowing is forbidden
even for a call-local instance.

**Module scope is immutable by a strict whitelist grammar.** A retained value
(module-level binding target, class attribute, function/lambda default, and any
value reachable from them) is allowed ONLY if its expression matches this
grammar; EVERYTHING else is a P3 violation, with no attempt to reason about
what an arbitrary expression evaluates to:

```text
  IMM := Constant (str, bytes, int, float, complex, bool, None, Ellipsis)
       | Tuple display whose elements are all IMM            (no Starred elements)
       | `frozenset(` Set/List/Tuple display of IMM `)`  or a frozenset({IMM, ...}) display form
       | `types.MappingProxyType(` Dict display with IMM keys and IMM values `)`   (no ** unpacking)
       | `re.compile(` Constant [, Constant-flags] `)`
       | Name / Attribute referring to a MODULE-LEVEL name in the same connector package that was itself bound
         to IMM (checked by the same grammar), or to an imported module/class/function
       | UnaryOp/BinOp over IMM constants only (e.g. -1, 60 * 60, "a" + "b")
       | Enum member reference (`Kind.FILE`) ONLY for a package-defined Enum with IMM member values
         and proved package-defined Enum ancestry, without metaclass= or class-building hooks
```

Binding forms: only simple `NAME = IMM`, `NAME: T = IMM`, or tuple-target unpacking whose RHS is a Tuple display
with the same arity and no Starred on either side. Anything else (starred targets/values, augmented assignment,
walrus at module scope, `for` at module scope, comprehensions, calls not listed above such as tuple(...), list(...),
sorted(...), dict(...), copy(...), any user function call) is a violation. Function definitions, class definitions,
imports, `__all__ = (IMM, ...)`, docstrings, `if TYPE_CHECKING:` blocks and type aliases remain allowed.

Capitalization and `__all__` give no exemption. Static imports/re-exports within
the package follow the referenced module's definitions, including every binding.
Unknown imported payloads, cycles and unproved Enum members fail closed.
References to declared functions/classes are code
declarations; they do not permit calling a user factory. Imported code is
identified from source declarations without executing imports. Unavailable
declarations fail closed; external library internals remain a review boundary.
No constructor call can retain a record or another class instance. Enum and
NamedTuple class definitions remain allowed. Enum member references must resolve
to a class inside the scanned package: external or unavailable Enum declarations,
unknown mixins, custom metaclasses, inherited class-building hooks, mutable
payloads and `auto()` fail closed. Operator operands must be constant expressions;
an immutable object with an overloaded operator does not qualify.

```python
from types import MappingProxyType

EXPORT_MIME = MappingProxyType({"application/example": "text/plain"})
__all__ = ("CONNECTOR",)
```

The former constructor/handle exemptions are removed. Even `tuple((1, 2))`,
`frozenset(ITEMS)`, a user function returning a tuple, a starred immutable tuple,
and `MappingProxyType(non_literal_dict)` fail. Use a tuple display, literal
frozenset contents and a literal proxy dict. Acquire logger handles, time/SDK
objects and other call results inside a function, as the shipped connectors do.
Unmatched/unproved unpacking reports violations for all affected names instead
of silently dropping them. Loop/context-manager/pattern bindings fail closed;
move them inside a function. `global`/`nonlocal` rebinding remains forbidden.

Class definitions may declare per-instance `dataclasses.field` metadata,
including factories, for **call-local** instances (Drive's `_Ancestry`). That
metadata is not an IMM default: all retained instance constructors are refused.
There is no factory-result inference. `ClassVar` is a retained class
attribute and cannot use this declaration metadata. Every function/lambda
default, including a factory lambda's defaults, still must match IMM.

Attribute stores/deletes and `setattr`/`delattr` fail closed for functions,
classes, shared singleton receivers and unknown targets, including aliases.
Direct instance-field writes inside methods of helper classes whose constructors
are created per call remain legal (for example, the per-call HTML
parser). Frozen declarations, Enum members and class methods do not receive that
exception. `setattr`/`delattr` on the direct instance receiver of a call-local
helper are allowed for ordinary attributes; unknown receivers fail closed.
Memoizing and unknown decorators are rejected; only dataclass/Enum metadata and the stateless method/type wrappers
(`staticmethod`, `classmethod`, `property`, `abstractmethod`, `overload`) are allowed.

The binding rule is the P3 guarantee: `.get()`, `.setdefault()`,
`next(iter(...))`, and helper-return borrowing cannot hide a mutable container
that was never allowed at module scope. Imported code declarations are explicit
trust boundaries, not proof of third-party payload immutability.
The existing lexical mutation detector remains secondary protection: known
mutators and static attribute/subscript stores/deletes rooted in module state
fail. Function/lambda defaults, decorators, annotations, class bases and
comprehension first iterables are checked in their enclosing scopes; a local
shadow in a nested helper or a class attribute cannot excuse a module mutation.
Local containers inside a function remain normal per-run state.

**What the scan does not catch.** Stated precisely, because "we disclosed it" is
not the same as "we pinned it" — everything below is review-caught, not
test-caught:

- **reflective/dunder tricks beyond the banned names**, dynamic import
  (`importlib.import_module(...)`, `__import__(...)`), computed `getattr`,
  `exec`/`eval`, `sys.modules`, and frame introspection. These deliberate evasions are outside this
  first-party guard rail. Static relative/parent imports, accessor aliases,
  settings-object binding and stored bound methods are checked. The same
  dynamic routes can also reach `app.db` directly;
- **external library internals.** Imported implementation internals and
  call-local logging infrastructure still require review. Locally constructed
  singleton payloads, ordinary instance writes on frozen entry points,
  function attributes, mutable defaults and aliases of those objects are
  checked by P3; they are not accepted residuals.

The scan closes what syntax can close. It is a lint, not a sandbox — which is
exactly why the trust model above says first-party, code-reviewed connectors
only, and why the documented dynamic/reflective limits belong in review.

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
@classmethod
def oauth_spec(cls) -> OAuthSpec: ...
```

Presence makes the source type **managed**: the framework drives the whole
authorization-code + PKCE flow, admin-gates every mutation (create, connect,
reconnect, sync-now, disconnect — checked against *current* roles, INV-5),
stores the refresh token in the CC-C vault, and refreshes it per run. You return
endpoints and registration only:

```python
from app.core.config import get_settings


class AcmeConnector:
    name = "acme"

    @classmethod
    def oauth_spec(cls) -> OAuthSpec:
        return OAuthSpec(
            authorize_url="https://accounts.example.com/o/oauth2/v2/auth",  # https
            token_url="https://oauth2.example.com/token",                   # https
            scopes=("https://www.example.com/auth/files.readonly",),        # non-empty
            client_id=get_settings().acme_oauth_client_id,
            client_secret=get_settings().acme_oauth_client_secret,
            allowed_hosts=("accounts.example.com", "oauth2.example.com", "api.example.com"),
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )
```

Add these deployment fields to `Settings` and explicitly classify both in
`ALLOWED_DEPLOYMENT_CONFIG` alongside the non-local startup validator and its
test (§8). Import the accessor unaliased and read each field directly (§3).

Conformance requires: both endpoints **https**, a non-empty `scopes`, a
non-empty `allowed_hosts` of bare lowercase hostnames, and that the endpoints
you declare are inside your own pinned set.

Optionally implement `fetch_account_email(http)` — the post-exchange identity
probe. A read-only content scope typically returns **no identity claim** with
its tokens, so the framework calls this over the guarded client right after the
exchange to learn (and audit) the connected account.

### 4.2 `fetch_changes()` — incremental sync

```python
@classmethod
async def fetch_changes(
    cls, source: Source, cursor: str, run: ConnectorRun
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
  accept back — conformance feeds your terminal cursor straight back in and
  applies the same page/domain-document validation to both replay drains.
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
@classmethod
def map_acl(cls, raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]: ...
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

**Shape:** lowercase `snake_case`, matching `^[a-z][a-z0-9_]*$`. That is not a
new rule — it is what all 72 `code="…"` literals in `backend/app/` already use,
and conformance now enforces it (there is no shared validator to defer to). A
padded or blank code fails: `" "` is a non-empty string and a useless
discriminator, since nothing can branch on it, search for it, or count it.

**This applies past `validate_config`.** Conformance drives your connector
against a *failing* provider and requires that `sync()` and `fetch_changes()`
surface a `ConnectorError` — a leaked `httpx.HTTPError` or `ValueError` becomes
a 500 with vendor detail instead of a recorded, safe `last_error`. And
`health()` **reports** a fault (`ConnectorHealth(healthy=False, detail=…)`)
rather than raising it: a raising probe turns one unreachable source into a
failed request for the whole connector grid.

**"Stable" is checked, not assumed.** Each fault is driven **twice** and the
code must be identical both times *and* equal to the value your harness declares
(`sync_fault_code`, and `changes_fault_code` if you declare `fetch_changes`).
A per-occurrence code (`f"drive_error_{uuid4()}"`) is non-empty and still
useless — nobody can match on it, search for it, or count it. Pin one code per
failure mode; changing it later is a visible diff in the harness, which is the
point.

Declaring the expected code is **mandatory**, not a nicety: a harness that omits
it fails `check_harness_completeness` rather than quietly downgrading the rule
to "the code did not vary". Every fixture a declared capability needs works this
way — an absent fixture is a failure, because a rule that silently checks less
is indistinguishable from a rule that passes.

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

1. **Drop the package in.** `CONNECTOR = YourConnector` in `__init__.py`. No
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
