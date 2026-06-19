# Lumen Copilot — Design Philosophy & System

> The reasoning behind the clickable wireframes in this folder. It explains **why**
> the product looks and behaves the way it does, and **how** the design system is
> built so anyone can extend it without drifting.
>
> Live prototype: open [`index.html`](index.html) in a browser (no build step — it
> runs from `file://`). These are **wireframes/prototypes**, not the production React
> app under [`frontend/`](../../frontend/).

---

## 1. First principle: the UI must argue the product

Lumen Copilot is a **multi-tenant enterprise Work-AI assistant** — grounded chat over
each user's connected sources and uploaded documents, where every answer is
**permissioned, cited, and auditable** ([spec 0003](../specs/0003-product-scope-and-mission.md)).

Enterprise buyers don't adopt an assistant because it's pretty; they adopt it because
they can **trust** it. So the design's job is not decoration — it is to make trust
*visible*. Every screen is built to surface the four mission filters from
[AGENTS.md §2](../../AGENTS.md), not bury them:

| Mission filter | How the UI makes it visible |
|---|---|
| **Permissioned by default** | A green `You have access` permission pill on every source and result; a `Restricted · masked fields` variant; an explicit "*N results hidden — you don't have access*" trim notice; access decisions in the audit log. |
| **Citation-backed** | Inline citation chips `[1]` in every answer that open a **source inspector** showing the exact passage (`<mark>`-highlighted) with owner, last-modified, last-indexed, and a deep link. "Cited" badges on direct answers. |
| **Read before write** | Drafts render with a `Draft — nothing is sent without your approval` callout; the Admin **Approvals & risk** tab maps each action to a risk tier (T0 auto → T3 dual-approval). |
| **Auditable** | A first-class Audit screen logs every retrieval / answer / access decision, with a provenance drawer that even shows *excluded* candidates and a raw event payload. |

A freshness pill, a retrieval trace ("Looked at 3 sources · 1,204 passages · 38
excluded"), and a model badge appear wherever an answer does. **If a pillar isn't
expressed on screen, the screen isn't done.**

---

## 2. Design tenets

1. **Calm, not flashy.** Generous whitespace, hairline borders, one accent, restrained
   shadows. The content (answers, evidence) is the hero; chrome recedes.
2. **Crisp at every density.** A strict 8px rhythm, a tight type scale, pixel-snapped
   1px borders, and visible focus rings. It should read as a serious tool, not a toy.
3. **Legible provenance.** Sources, freshness, and permissions are never more than a
   glance away. Monospace is used for identifiers (event ids, counts) so they scan.
4. **One system, many looks.** Theme, mode, accent, density, layout, and font are
   independent axes. A screen authored once renders correctly in *every* combination,
   because nothing is hard-coded — see §5.
5. **The user owns the surface.** Appearance is a per-device preference, not a fixed
   brand. The **Appearance & preferences** panel puts that control one click away.
6. **Accessible by default.** Keyboard focus, reduced-motion, color-scheme, and
   contrast are handled in the base layer, not bolted on per screen.

---

## 3. Information architecture

The MVP surfaces (mapped to the epics/cross-cuttings in
[consolidated-structure.md](../product/consolidated-structure.md)):

| Screen | Role | Load-bearing pillar |
|---|---|---|
| [Assistant](chat.html) | **Hero.** Grounded chat: retrieval trace, inline citations, source inspector, knowledge-mode chips, model picker, composer. | Citations + permissions |
| [Search](search.html) | Unified permissioned search: a cited direct answer above ranked results with "why it matched" and permission-trimming. | Permissions + citations |
| [Documents](documents.html) | Upload (parse → chunk → embed → index), collections, ingest status, viewer drawer. | Read-before-write, freshness |
| [Sources](sources.html) | Connector grid: sync health, indexed counts, permission-mirror status, failure/reconnect states. | Permissions, freshness |
| [Audit log](audit.html) | Every retrieval / answer / access decision; provenance drawer with excluded candidates + raw event. | Auditable |
| [Admin](admin.html) | Members & roles, model governance (gateway), data-minimization toggles, approval risk tiers. | Governed, read-before-write |
| [Sign in](login.html) | Tenant-aware SSO front door. | — |
| [Foundations](reference/foundations.html) · [Components](reference/components.html) | Living design reference / UI kit. | — |

The **shell** is consistent across screens: a brand cell, a top bar (command/search,
appearance control, tenant pill, account), and a left navigation rail — all injected by
`app.js` so every screen stays in lock-step.

---

## 4. The appearance system (user-controllable)

Trust also means *fit*: a SOC analyst at midnight and a salesperson in a bright office
want different surfaces. The **Appearance & preferences** panel (top-bar control, the
launcher, or ⌘/Ctrl-K) exposes six independent axes, **saved per-device** in
`localStorage`:

- **Theme (7):** Aurora, Graphite, Meridian, Indigo, Sunset, Forest, Slate — each a
  distinct color *identity* (accent + neutral temperature).
- **Mode:** Light · Dark · System (follows the OS, live via `prefers-color-scheme`).
- **Accent:** override any theme's accent from a swatch; shades are *derived* (see §5),
  so overrides behave correctly in both modes. "Reset" restores the theme default.
- **Density & spacing:** Compact · Cozy · Comfortable.
- **Navigation & layout:** Full nav · Icons-only rail · Centered top nav.
- **Interface font:** Inter · System · Rounded.

A **live preview** inside the panel reflects every change instantly. This is deliberate:
appearance changes should be *legible before commitment*.

### The 7 themes — intent

| Theme | Identity | Best for |
|---|---|---|
| **Aurora** | Indigo → violet, cool neutrals | The bright, default enterprise SaaS look |
| **Graphite** | Sky blue, pure neutral grays, hairlines | High-density, "just the facts" power use |
| **Meridian** | Green → gold, warm earthy neutrals | A warmer, editorial feel |
| **Indigo** | Deep saturated violet on navy | A bolder, branded variant |
| **Sunset** | Orange → red, warm maroon | High-energy, distinctive |
| **Forest** | Green on deep green | Calm, natural |
| **Slate** | Monochrome gray-blue | Maximum neutrality / focus |

---

## 5. Foundations

### Color & theming — two axes, derived accents

The token system has **two independent axes**: `[data-theme]` (color identity) ×
`[data-mode]` (light/dark). Semantics (`--ok/--warn/--danger/--info`), shadows, and the
backdrop live at the **mode** level (they're mostly mode-dependent, not theme-dependent),
which keeps the palette set lean — see [`assets/tokens.css`](assets/tokens.css).

Each theme×mode block sets only its neutrals + a default `--accent`. Every accent shade
is **derived from `--accent` with `color-mix`** in `:root`:

```css
--accent-strong:  color-mix(in srgb, var(--accent), var(--text) 22%); /* mode-aware */
--accent-weak:    color-mix(in srgb, var(--accent) 13%, var(--surface));
--accent-line:    color-mix(in srgb, var(--accent) 38%, var(--surface));
--accent-grad:    linear-gradient(135deg, …);
--ring:           color-mix(in srgb, var(--accent) 45%, transparent);
```

Because the shades are derived, a **user accent override** is just one inline
`--accent` on `<html>` — everything (buttons, chips, citation markers, focus rings,
gradients, the page glow) recolours automatically, in both light and dark.

### Sizing — three scale multipliers

Three multipliers on `<html>` drive the whole component library:

- `--fs` — text size · `--space` — spacing/density · `--radius` — roundness

Component CSS expresses dimensions as `calc(px * var(--…))`, so the density presets move
all three at once. Control heights use `calc(px * max(var(--fs), var(--space)))` so text
never clips when scaled up.

### Type, space, elevation, motion, icons

- **Type:** Inter (UI) + JetBrains Mono (identifiers/counts); a display family per the
  Interface-font choice. Tight scale (`--t-xs`…`--t-3xl`), `-0.01…-0.02em` display
  tracking.
- **Spacing:** 8px rhythm via `calc(px * var(--space))` utilities.
- **Radius:** `--r-xs`…`--r-xl` + `--r-full` (pills), all scaled by `--radius`.
- **Elevation:** three shadow steps (resting · raised · floating) tuned per mode.
- **Motion:** one easing curve + three durations; **honors `prefers-reduced-motion`**.
- **Icons:** a single inline SVG sprite injected by `app.js` (no network, works on
  `file://`); 1.7px stroke, scales with `--fs`.

Browse them live in [Foundations](reference/foundations.html).

---

## 6. Signature components & patterns

These are the patterns that make the product legible (see the full kit in
[Components](reference/components.html)):

- **Citation chip + source inspector** — `[n]` markers link to a passage panel with the
  quoted text highlighted, owner/freshness/visibility, and "Open in source".
- **Permission pill** (`.perm` / `.perm-restricted`) — the most-repeated trust signal.
- **Freshness pill** (`.fresh` / `.fresh-stale`) — recency at a glance.
- **Retrieval trace** — a collapsible record of what was searched, ranked, and excluded.
- **Source result row** — app glyph, title, snippet with match highlight, "why it
  matched", owner, freshness, permission.
- **Audit row + provenance drawer** — the proof surface: per-candidate allow/exclude
  decisions and a raw event payload.
- **Status / sync** dots, **risk-tier** badges (T0–T3), **KPI** cards, and a **command
  palette** (⌘/Ctrl-K) for fast navigation.

---

## 7. Accessibility

- Visible **focus rings** on every interactive element (`:focus-visible`, accent ring).
- **`prefers-reduced-motion`** collapses animations/transitions globally.
- **`color-scheme`** is set per mode so native controls/scrollbars match.
- Contrast: accents and `--accent-contrast` are chosen so text on accent stays legible
  in both modes; semantic colors meet practical contrast on their tinted backgrounds.
- Targets follow the 8px rhythm; control heights never collapse below a tappable size.

---

## 8. Implementation notes

```
docs/wireframes/
  index.html              launcher / look gallery + appearance entry
  chat · search · …       one HTML file per screen (semantic markup only)
  reference/*.html        Foundations + Components (living reference)
  assets/
    tokens.css            7 themes × light/dark + scale multipliers (the "options")
    app.css               component library — pure token-driven, re-skins automatically
    app.js                shared chrome, appearance panel, command palette, interactions
  README.md               how to run + the screen index
  DESIGN.md               this document
```

- **No build, no dependencies.** Open any file directly. Fonts come from a Google Fonts
  `<link>` with system fallbacks; the Rounded font loads on demand.
- **Screens are thin.** A screen page is mostly semantic markup; the shared chrome,
  theming, icons, and interactions come from `app.js` / `app.css`, so all screens move
  together.
- **Interactions are real but scripted** — citations open the inspector, the composer
  returns a canned grounded answer, uploads simulate ingest, drawers/modals/tabs work —
  enough to *walk* the system, not to wire a backend.

---

## 9. How to extend without drifting

- **Add a theme:** add a `[data-theme="x"][data-mode="light"]` and `…["dark"]` block in
  `tokens.css` (neutrals + `--accent` only), then one entry in the `THEMES` array in
  `app.js`. Nothing else changes — derived shades and every screen follow.
- **Add a screen:** copy a screen's `<head>`/shell boilerplate, set `data-screen`, write
  the `<main>` using existing component classes, and add a nav entry in `app.js`.
- **Rule:** never hard-code a color, shadow, or radius in a screen — always use a token
  / CSS variable, so theme · mode · accent · scale keep working.

---

## 10. Non-goals

- Not the production UI — it informs [`frontend/`](../../frontend/) (which already uses a
  matching RGB-triple token approach) but is not wired to the API.
- Not a full design-system package (no component framework, tests, or Storybook).
- Not exhaustive of the 16-epic roadmap — it covers the **M0–M1 MVP** surfaces only.

---

## Provenance

- **Audience & scope:** decided in-session with the human sponsor; grounds itself in
  [spec 0003 (scope & mission)](../specs/0003-product-scope-and-mission.md),
  [AGENTS.md §2](../../AGENTS.md), and the discovery corpus in [`docs/product/`](../product/).
- **Status:** design exploration / clickable wireframes — input for the production UI,
  not a closed decision.
