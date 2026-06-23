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

> The remaining open decisions — security & domain invariants (OD-4), CI (OD-7), and the rest of the `.claude/` harness (OD-6) — are tracked in [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md). Each costly, non-obvious choice gets its own ADR before code lands.
