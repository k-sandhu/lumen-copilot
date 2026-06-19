# Lumen Copilot — Clickable System Wireframes

Static, dependency-free HTML wireframes for the Lumen Copilot MVP — a multi-tenant
enterprise Work-AI assistant: grounded chat over connected sources + uploaded documents,
where every answer is **permissioned, cited, and auditable**.

> **No build step.** Open [`index.html`](index.html) in any browser (double-click works —
> everything runs from `file://`).

> **Why it looks/works this way:** see [`DESIGN.md`](DESIGN.md) — the holistic design
> philosophy & system (principles, the trust-signal patterns, the theme/token model).

## Start here

[`index.html`](index.html) is the launcher. It lets you:

- **Pick a look** (applies live across every screen, remembered via `localStorage`).
- **Jump into any screen** of the prototype.

## Appearance & preferences

The whole app re-skins instantly. Open the **Appearance & preferences** panel (top-bar
control, the launcher button, or **Ctrl/Cmd-K**) — every choice is saved per-device in
`localStorage`.

**Themes** — 7 identities, each with a **light and dark** mode:

| Theme | Feel |
|---|---|
| **Aurora** | Indigo → violet, cool neutrals |
| **Graphite** | Sky blue, pure neutral grays, hairlines |
| **Meridian** | Green → gold, warm earthy neutrals |
| **Indigo** | Deep saturated violet on navy |
| **Sunset** | Orange → red, warm maroon |
| **Forest** | Green on deep green |
| **Slate** | Monochrome gray-blue |

- **Mode:** Light · Dark · System (follows the OS, live).
- **Accent:** override any theme's accent from a swatch row (or Reset to the theme default).
  Shades are derived with `color-mix`, so overrides work in both modes.
- **Density & spacing:** Compact · Cozy · Comfortable (drives `--fs` / `--space` / `--radius`).
- **Navigation & layout:** Full nav · Icons only · Centered (top nav).
- **Interface font:** Inter · System · Rounded.
- **Live preview** inside the panel reflects every change instantly.

Everything is driven by CSS variables (theme × mode token sets + `--accent`, `--font-sans`,
`--fs`, `--space`, `--radius`), so one screen renders in any combination.

## Screens

| Screen | What it shows |
|---|---|
| [Assistant](chat.html) | **Hero** — grounded chat: inline citations, retrieval trace, source inspector, knowledge modes, model picker. Type in the composer to see a scripted grounded reply. |
| [Search](search.html) | Unified permissioned search: a cited direct answer + ranked results with "why it matched" and permission-trimming. Type to filter live. |
| [Documents](documents.html) | Upload (click the dropzone to simulate ingest), collections, ingest status, and a viewer drawer with cited passages. |
| [Sources](sources.html) | Connector grid: sync health, indexed counts, permission-mirror status; add-source modal. |
| [Audit log](audit.html) | Every retrieval / answer / access decision; click a row for a full provenance drawer. |
| [Admin](admin.html) | Members & roles, model governance, data-minimization toggles, approval risk tiers. |
| [Sign in](login.html) | Tenant-aware SSO front door. |

## Design reference (Figma-like)

- [Foundations](reference/foundations.html) — color tokens across all looks, type scale, spacing, elevation, icons.
- [Components](reference/components.html) — the full UI kit / living documentation.

## How it's built

```
index.html              launcher / look gallery
chat / search / …       the screens (one HTML file each)
reference/*.html         design reference pages
assets/
  tokens.css            the 3 looks as [data-theme] token sets (the "options")
  app.css               component library — pure token-driven, re-skins automatically
  app.js                shared chrome (top bar + nav), theme switch, command palette,
                        and all the data-attribute interactions (no dependencies)
```

Every color, shadow, and radius is a CSS variable, so a screen written once renders in
all three looks. Icons are injected inline by `app.js` (no network needed).

> These are **wireframes/prototypes**, not the production React app under `frontend/`.
> They exist to compare visual directions and walk the end-to-end system before build.
