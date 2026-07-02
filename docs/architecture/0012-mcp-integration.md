# 12. MCP server integration — transport, module boundary, egress

- **Status:** Accepted *(sponsor-delegated decision; the spike recommendations of [#203](https://github.com/k-sandhu/lumen-copilot/issues/203) are adopted as the decision, 2026-07-02)*
- **Date:** 2026-07-02
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries — new external system ⇒ new module + a boundary-table row), [ADR-0009](0009-connector-framework-and-web-source.md) (the `connectors/web/fetch.py` SSRF chokepoint — the egress template), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenant isolation, INV-2 owner-or-grant, INV-5 authz, INV-6 audit, INV-7 read-before-write, §2.5 risk tiers)
- **Coordinates with:** [#207](https://github.com/k-sandhu/lumen-copilot/issues/207) (CC-A — the agent tool registry MCP tools plug into), [#209](https://github.com/k-sandhu/lumen-copilot/issues/209) (CC-C — the encrypted per-tenant secrets vault), ADR-0013 *(code-execution sandbox — [#204](https://github.com/k-sandhu/lumen-copilot/issues/204), the prerequisite for local/stdio MCP servers; proposed in parallel)*
- **Tracking:** [#203](https://github.com/k-sandhu/lumen-copilot/issues/203) (this spike) · epic [#199](https://github.com/k-sandhu/lumen-copilot/issues/199) · program [#196](https://github.com/k-sandhu/lumen-copilot/issues/196)

## Context

A tenant's assistants should be able to use tools exposed by **MCP (Model Context Protocol) servers** — the emerging open standard for connecting AI applications to external tool/resource providers — but only **under Lumen's existing governance**: the per-assistant allow-list, risk tier, approval hook, audit trail, and egress controls that already gate native tools. MCP is a **new external system**, so per [ADR-0004](0004-architecture-boundaries-and-adapters.md) §6 it needs **one owning module and a boundary-table row**: "the only place that talks to MCP." Without that, an MCP JSON-RPC client leaks into routers and services and the governance chokepoints (permission, audit, egress) become unprovable — exactly the failure ADR-0004 exists to prevent. This ADR draws that boundary and fixes the load-bearing security decisions before any adapter code lands.

Two facts shape every decision here:

1. **MCP servers run tools we do not control**, discovered at runtime. A discovered tool may write, send, or delete. So the default trust posture must be *low*: an unknown external tool is at least **T2 (approval-gated)** until proven read-only ([spec 0004 §2.5](../specs/0004-security-and-domain-invariants.md)), its arguments are schema-validated before invoke, and its failures are contained.
2. **Remote MCP servers are user-supplied HTTP endpoints.** The server fetches them, which is the same server-side-request-forgery exposure ADR-0009 already solved once. Reusing that chokepoint — rather than re-deriving it — is mandatory; a bypass is a blocking defect at the same bar as the web connector.

The MVP is otherwise **T0** (read-only). This ADR does not add a T2+ tool to the shipping product on its own — it builds the seam through which the *first* governed external tools arrive, gated by the CC-A approval hook ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)), which stays inert until a T2+ tool is actually granted.

## Decision

### 1. Transports (v1) — **remote only**; stdio/local-process **deferred**

| Transport | v1 | Why |
|---|---|---|
| **Streamable HTTP** *(primary)* | **Yes** | The current remote MCP transport; a single HTTP endpoint, reuses the ADR-0009 egress chokepoint verbatim, no host-process execution |
| **HTTP + SSE** *(compat)* | **Yes** | The earlier remote transport still shipped by many servers; same egress discipline, kept for compatibility |
| **stdio / local-process** | **No — deferred** | Executes a vendor binary **on our host**; same isolation problem as agent-authored code |

Only **remote** transports ship in v1, because they need no host-process execution and their entire attack surface is an outbound HTTP connection we already know how to constrain (§4).

**stdio / local-process MCP servers are deferred.** A stdio server means Lumen spawns and runs a third-party binary on the worker host — an arbitrary-code-execution exposure identical in kind to the agent-authored-code problem. It **must wait for the code-execution sandbox (ADR-0013, [#204](https://github.com/k-sandhu/lumen-copilot/issues/204)/[#200](https://github.com/k-sandhu/lumen-copilot/issues/200))**; only inside that isolation boundary is running a local MCP server acceptable. Until then, registering a stdio transport is rejected at the boundary (`transport` is constrained to the remote values). This matches epic [#199](https://github.com/k-sandhu/lumen-copilot/issues/199)'s scope fence.

### 2. Client — the **official MCP Python SDK**, behind the new `backend/app/mcp/` module

Use the **official MCP Python SDK** as the client, contained entirely within `backend/app/mcp/`. This is a deliberate, documented exception to ADR-0004's "prefer an HTTP boundary to a vendor SDK" preference — the exception the rule allows ("*don't reach for a vendor SDK when an HTTP boundary will do*" — here it does **not** do): MCP is a versioned protocol (capability negotiation/handshake, JSON-RPC framing, the Streamable-HTTP and SSE transports, tool/resource schemas, protocol-version headers) whose **correctness and forward-compatibility** are exactly what a maintained reference implementation buys. Hand-rolling the JSON-RPC/transport layer would re-implement a moving spec and drift from it. The SDK earns its place; it is confined to one module so the lock-in is localized and swappable.

**Vendor types never leak upward** ([ADR-0004](0004-architecture-boundaries-and-adapters.md) adapter rule). No SDK object, JSON-RPC frame, or MCP schema type crosses the `mcp/` boundary. The module exposes **domain types only** — the same `ToolSpec` / `ToolResult` vocabulary the CC-A registry ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) already speaks, so a discovered MCP tool is indistinguishable to callers from a native one. Nothing else in the app imports the MCP SDK.

**Module API (`backend/app/mcp/`):**

- `connect(server) -> Session` — open + handshake/capability-negotiate a transport (Streamable HTTP or SSE) to a registered server, through the egress guard (§4). Auth material is fetched from CC-C at connect time (§3), never held in the registry row.
- `list_tools(server) -> list[ToolSpec]` — discovery: the server's advertised tools normalized to domain `ToolSpec` (name, description, args JSON-schema, read-only/destructive annotations if present).
- `call_tool(server, name, args) -> ToolResult` — invoke one tool; returns a typed domain `ToolResult`. Validates `args` against the advertised JSON schema **before** the call (§5) and contains failures (§6).
- `health(server) -> HealthStatus` — a bounded liveness/handshake probe used by registration and the management UI.

### 3. Boundary-table row — **PROPOSED (needs human approval of `AGENTS.md` §6)**

Per ADR-0004 §6, a new external system requires a new module **and** a new row in the boundary table, added in the same change. The exact row to add:

| Concern | Single owning module |
|---|---|
| **MCP servers** (tool discovery + invoke) | `backend/app/mcp/` (official MCP SDK client; exposes domain `ToolSpec`/`ToolResult` only) |

This ADR **records** that row and extends the ADR-0004 §6 table — exactly as [ADR-0010](0010-dedicated-text-search-engine.md) §3 recorded the `search/` boundary shift (ADR-0004 is immutable; it is extended here, not rewritten). **`AGENTS.md` §6 and the mirrored ADR-0004 §6 table are edited only when a human approves this ADR** — per [AGENTS.md](../../AGENTS.md) §5 (self-modification gate) and §7.6, agents do not edit `AGENTS.md`. The adapter PR ([#225](https://github.com/k-sandhu/lumen-copilot/issues/225)) that lands `backend/app/mcp/` carries the mirrored-table edit for human sign-off in the same change.

### 4. Egress control (load-bearing, `risk:security`) — reuse the ADR-0009 SSRF chokepoint

A remote MCP server is a **user-supplied endpoint the server connects to**, so every outbound MCP connection MUST pass through the **same SSRF discipline as [`connectors/web/fetch.py`](../../backend/app/connectors/web/fetch.py)** ([ADR-0009](0009-connector-framework-and-web-source.md) §3). A bypass is a **blocking defect**. Specifically, every MCP connection (handshake, each request, and — critically — **every redirect hop**):

- **https-only** — reject any non-`https` scheme for a remote server (stricter than the web connector's http/https, because MCP carries credentials);
- **rejects blocked address ranges on every hop** — resolve the host and refuse loopback, private (RFC-1918 / IPv6 ULA), link-local, CGNAT (100.64.0.0/10), and the **cloud-metadata** address (169.254.169.254); a host resolving to *any* blocked address is refused (no partial allow); re-validate the target of **each redirect** and never follow to a blocked one; pin the connection to the validated IP to close the DNS-rebind (TOCTOU) window;
- **bounds every call** — wall-clock timeouts, a redirect-hop cap, and response **size caps** enforced while streaming;
- **descriptive `User-Agent`** — a version-stamped Lumen UA on every request;
- **per-tenant rate limit** on MCP egress.

**Reuse, don't re-derive.** The IP-range predicate, per-hop redirect re-validation, and IP-pinning already exist in `connectors/web/fetch.py`; the MCP client MUST call that shared guard (extracting the range-check/pinning helpers into a reusable egress primitive if needed) rather than reimplement it — one place to get SSRF right, one place with the negative tests. Because the MCP SDK owns its transport/redirect handling, the adapter injects an egress-guarded HTTP client / connection factory into the SDK (validated-IP pinning + per-hop re-validation) so the guard is *on the path the SDK actually uses*, not merely a pre-check that the SDK's own redirects could bypass. If the SDK cannot be made to route through the guard, that transport is not shipped.

**Optional admin endpoint allowlist.** An admin-configured allowlist of permitted MCP endpoint hosts (per tenant) is supported as an *additional* deny-by-default narrowing on top of the SSRF guard — it never *widens* access (an allowlisted host still passes the full range check). Not required for v1; the SSRF chokepoint is the mandatory control, the allowlist is defense-in-depth an admin may opt into.

### 5. Registration + secrets — tenant/owner-scoped `mcp_servers`, credentials via CC-C

Registered MCP servers live in a tenant/owner-scoped **`mcp_servers`** table (per [#203](https://github.com/k-sandhu/lumen-copilot/issues/203)):

| Column | Notes |
|---|---|
| `id` | pk |
| `tenant_id` | FK, indexed — tenant scope (INV-1); RLS-backed |
| `owner_id` | FK — the registering user (INV-2) |
| `name` | display name |
| `transport` | enum: `streamable_http` \| `sse` — **stdio rejected** (§1) |
| `endpoint_url` | the remote endpoint (https; SSRF-checked on register **and** every connect) |
| `auth_ref` | → a CC-C `SecretRef` ([#209](https://github.com/k-sandhu/lumen-copilot/issues/209)); **never the secret itself** |
| `enabled` | tenant/owner toggle |
| `status` | `pending` \| `ready` \| `error` (from `health`) |
| `last_health_at` | last successful probe |
| `discovered_tools` | jsonb — the last `list_tools()` snapshot (names + schemas + annotations) |
| `created_at` | |

**Credentials go through CC-C ([#209](https://github.com/k-sandhu/lumen-copilot/issues/209)), never this table.** The row stores only an **`auth_ref`** (a CC-C `SecretRef`); the actual token/header is envelope-encrypted in the `secrets` vault, **write-only**, and **never returned** through any API (a masked hint only — CC-C AC-3). The `mcp/` client fetches the plaintext **in-process** from `secrets_service.get_secret_plaintext(auth_ref)` at connect time and attaches it to the transport; it is never logged, never serialized into `discovered_tools`, and never crosses the API. Registration, health, and every mutation emit audit events (INV-6). A cross-tenant or non-owned `mcp_servers` id → **404** (INV-1/INV-2), same as every other tenant-scoped resource.

### 6. Tools into the CC-A registry — namespaced, tiered, schema-validated

Discovered tools are injected into the CC-A tool registry ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) so they flow through the **same** invoke → allow-list → approval → audit → trace path as native tools — MCP adds no second tool pipeline.

- **Namespacing.** Each discovered tool registers as **`mcp:<server_slug>:<tool>`** (server slug derived from `mcp_servers`, stable per server). The namespace prevents collisions between servers and between MCP and native tools, and makes provenance legible in the allow-list, trace, and audit. A per-assistant allow-list (E6-2) grants specific namespaced tools; an off-list call yields a typed `tool_not_permitted` result (CC-A), the run continues.
- **Risk tier — default T2 (approval-gated).** An MCP tool defaults to **T2** (consequential/external write → approval-gated per [spec 0004 §2.5](../specs/0004-security-and-domain-invariants.md) / INV-7) **unless** the server annotates it read-only / non-destructive (e.g. an MCP `readOnlyHint`-style / non-destructive annotation), in which case it maps to **T0**. Unknown/unannotated ⇒ **T2** — trust is earned, never assumed. A tenant admin may *raise* a tool's tier but not silently lower an unannotated tool below T2. T2/T3 tools route through the CC-A approval hook before invoke (inert for T0).
- **Schema validation before invoke.** `call_tool` validates the model-supplied `args` against the tool's **advertised JSON schema** before any outbound call; a schema violation is a typed `ok=false` `ToolResult` (the model sees it, the run continues) — malformed input is rejected at the boundary (INV-8), and a malformed call never reaches the server.
- **Audit + trace (via CC-A).** Every MCP invocation emits `tool.invoked` / `tool.result` through the existing audit sink and a `tool_invocations` row (server + tool + **arg-hash**, never raw args/secrets), and surfaces in the trace — the same records CC-A already writes for native tools (INV-6). No separate MCP audit path.

### 7. Failure isolation — a down/erroring/slow server never crashes the run

Server down, tool error, protocol fault, or timeout → a **typed `ToolResult(ok=False, error=…)`** with a safe message, **never an exception that crashes the chat run** — identical to CC-A's tool-failure contract ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207) AC-5). A per-call timeout bounds a slow server; a failed handshake marks the server `status=error` (`last_health_at`) and surfaces health in the management UI; the model receives the `ok=false` result and can proceed or apologize. Tenant isolation holds under failure: one tenant's broken MCP server never affects another's runs.

## Consequences

- **Governed extensibility.** A tenant registers a remote MCP server → its tools appear in the registry namespaced → an assistant is granted specific tools → chat invokes them with results in the trace + audit — all through the *existing* allow-list / approval / audit / egress chokepoints. No new governance surface, no bypass.
- **SSRF handled once.** MCP egress reuses the ADR-0009 chokepoint (extended to https-only + credential-carrying), with its negative tests, before any second server type exists — the one real risk of connecting to user-supplied endpoints is contained at a single seam.
- **The `mcp/` boundary is the whole coupling.** The MCP SDK lives in exactly one module behind domain `ToolSpec`/`ToolResult`; swapping the SDK or adding stdio later is a localized change. The cost is the proposed `AGENTS.md` §6 / ADR-0004 §6 row (human-approved on merge).
- **Low-trust default is deliberate friction.** Defaulting unknown MCP tools to **T2 (approval-gated)** means a freshly registered server's write-capable tools do not silently run — read-only tools are T0 and frictionless; anything unproven is gated. This is the read-before-write mission filter applied to third-party tools.
- **Deferred surface is explicit.** **stdio/local-process MCP servers do not ship until ADR-0013's sandbox exists** — recorded here so a later implementer does not add host-process execution without the isolation boundary. Shipping Lumen's *own* data as an MCP server to external hosts (the server side of E15-4) is separately out of scope.
- **New hard dependency, contained.** The official MCP SDK is a new backend dependency, justified by protocol correctness and confined to `backend/app/mcp/`; if it lags the spec or is abandoned, the blast radius is one module.
- **Delivery (ADR-0008 shape).** This ADR is the serialized seam; the epic then builds in dependency order: `#224` contract freeze → `#225` client adapter + egress (lands `backend/app/mcp/` **and** the boundary-row edit) → `#226` registration + secrets + health ‖ `#227` tools-into-registry → `#228` management UI. Each is its own issue/PR with `Closes #`.

## Unblocked issues

This ADR unblocks (flip their `blocked-by`): **[#224](https://github.com/k-sandhu/lumen-copilot/issues/224)** (MCP wire — contract freeze), **[#225](https://github.com/k-sandhu/lumen-copilot/issues/225)** (client adapter + egress), **[#226](https://github.com/k-sandhu/lumen-copilot/issues/226)** (registration + secret storage + health), **[#227](https://github.com/k-sandhu/lumen-copilot/issues/227)** (tools into the registry — namespaced/tiered/gated), **[#228](https://github.com/k-sandhu/lumen-copilot/issues/228)** (management UI).

## Resolved decisions (sponsor-delegated, 2026-07-02)

1. **Transports:** remote **Streamable HTTP** (primary) + **SSE** (compat); **stdio/local-process deferred** behind the ADR-0013 sandbox.
2. **Client:** the **official MCP Python SDK**, confined to `backend/app/mcp/`, exposing domain `ToolSpec`/`ToolResult` only (documented exception to the ADR-0004 SDK-avoidance preference — protocol correctness justifies it).
3. **Boundary row (proposed, needs human approval of `AGENTS.md` §6):** "MCP servers (tool discovery + invoke) → `backend/app/mcp/`".
4. **Registration/secrets:** tenant/owner-scoped `mcp_servers`; credentials via **CC-C** as an `auth_ref`, never returned.
5. **Egress:** reuse the `connectors/web/fetch.py` SSRF chokepoint — https-only, per-hop range checks (loopback/private/link-local/metadata), IP-pinning, timeouts, size caps, descriptive UA, per-tenant rate limit; optional admin allowlist.
6. **Registry:** namespaced `mcp:<server_slug>:<tool>`; default **T2 (approval-gated)**, read-only-annotated ⇒ **T0**; args JSON-schema-validated before invoke.
7. **Failure isolation:** every failure mode → typed `ToolResult(ok=false)`; the run never crashes.
