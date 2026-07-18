# Architecture Decision Records

We record decisions that are **costly to reverse and not self-evident from the code** (Nygard format: *Context / Decision / Consequences*). One file per decision, `NNNN-kebab-title.md`, sequentially numbered, indexed below.

**Accepted ADRs are immutable — you supersede, you don't rewrite.** A changed decision is captured by a *new* ADR that supersedes the old one.

**When to write one:** a technology or boundary choice, a licensing constraint, a deliberate scope cut.
**When not to:** routine, reversible choices that are obvious from the code.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-multi-harness-agent-roles.md) | Multi-harness agent role model | Accepted |
| [0003](0003-application-stack.md) | Application stack (closes OD-2) | Accepted |
| [0004](0004-architecture-boundaries-and-adapters.md) | Architecture boundaries & adapters (closes OD-3) | Accepted |
| [0005](0005-local-run-and-developer-workflow.md) | Local-run path & developer workflow (closes OD-5) | Accepted |
| [0006](0006-contract-first-parallel-implementation.md) | Contract-first, parallel front/back implementation | Accepted |
| [0007](0007-adopt-wireframe-ia-and-design-system.md) | Adopt wireframe IA & design system as the production UI target | Accepted |
| [0008](0008-conflict-free-parallel-delivery.md) | Conflict-free parallel delivery (vertical slices, serialized seams, auto-discovery) — extends 0006 | Accepted |
| [0009](0009-connector-framework-and-web-source.md) | Connector framework + Web URL source (first connector) | Accepted |
| [0010](0010-dedicated-text-search-engine.md) | Dedicated text-search engine (OpenSearch, single store) behind the retrieval seam | Accepted |
| [0011](0011-assistant-and-agent-runtime.md) | Assistant & agent-runtime — configured single-agent chat (reuses `chat_runtime`) | Accepted |
| [0012](0012-mcp-integration.md) | MCP server integration — transport, module boundary (`backend/app/mcp/`), egress | Accepted |
| [0013](0013-code-execution-sandbox.md) | Code-execution sandbox for agent-authored Python (container-per-run via a `sandbox-runner` service) | Accepted |
| [0014](0014-web-search-provider.md) | Web-search provider & egress (`web_search` agent tool — self-hosted SearXNG) | Accepted |
| [0015](0015-scheduling-and-headless-runs.md) | Scheduling & headless agent runs (celery-redbeat + a persisting run sink) | Accepted |
| [0016](0016-context-engine-and-cache-first-prompting.md) | Context engine & cache-first prompting (assembler, caching, compression, resilience, tool concurrency) | Accepted |
| [0017](0017-hierarchical-memory.md) | Hierarchical memory (org/assistant/user) with governed promotion | Accepted |
| [0018](0018-sub-agent-orchestration.md) | Sub-agent orchestration (orchestrator–worker fan-out; feeds the research epic) | Accepted |
| [0019](0019-connector-sdk-and-oauth.md) | Connector SDK, OAuth & ACL mirroring (first managed connector: Google Drive) | Accepted |
| [0020](0020-reusable-root-sandbox-sessions.md) | Reusable root-capable Python sandbox sessions | Accepted |

> The remaining open decisions — CI (OD-7) and the rest of the `.claude/` harness (OD-6 remainder) — are tracked in [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md). (Security & domain invariants (OD-4) closed 2026-06-18 by [spec 0004](../specs/0004-security-and-domain-invariants.md).) Each costly, non-obvious choice gets its own ADR before code lands.
