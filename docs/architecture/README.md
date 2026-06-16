# Architecture Decision Records

We record decisions that are **costly to reverse and not self-evident from the code** (Nygard format: *Context / Decision / Consequences*). One file per decision, `NNNN-kebab-title.md`, sequentially numbered, indexed below.

**Accepted ADRs are immutable — you supersede, you don't rewrite.** A changed decision is captured by a *new* ADR that supersedes the old one.

**When to write one:** a technology or boundary choice, a licensing constraint, a deliberate scope cut.
**When not to:** routine, reversible choices that are obvious from the code.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |

> The first product-shaping ADRs (stack, boundaries, local-run path) are still open — see [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md). Each gets its own ADR before code lands.
