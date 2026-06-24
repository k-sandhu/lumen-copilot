/**
 * Regression guard for the design-token layer (issue #130).
 *
 * The frontend has two consumers of the color tokens: Tailwind (needs RGB
 * triples for `rgb(var(--x) / <alpha-value>)`) and the @/ui kit (needs full
 * colors). They previously collided — `tokens.css` set hex `--surface`/`--accent`
 * which shadowed the RGB-triple `--surface`/`--accent` in `index.css`, so every
 * `*-accent` / `*-surface` Tailwind utility produced `rgb(#hex / α)` → invalid →
 * transparent (the invisible "Sign in" button).
 *
 * The fix: a SINGLE source of truth. `--c-*` RGB triples feed Tailwind; the kit's
 * full-color vars are DERIVED via `rgb(var(--c-*))`. These tests fail if anyone
 * reintroduces the collision (a hex token def, or Tailwind pointing back at the
 * un-prefixed vars). They parse the source files, so no DOM/CSSOM is needed.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(join(here, 'tokens.css'), 'utf8');
const indexCss = readFileSync(join(here, 'index.css'), 'utf8');
const tailwindConfig = readFileSync(join(here, '..', '..', 'tailwind.config.ts'), 'utf8');

const THEMES = ['aurora', 'graphite', 'meridian', 'indigo', 'sunset', 'forest', 'slate'];
const TAILWIND_COLORS = [
  'surface',
  'surface-muted',
  'border',
  'foreground',
  'foreground-muted',
  'accent',
  'ok',
  'warn',
  'danger',
];

describe('design tokens — single source of truth (#130)', () => {
  it('defines all 7 themes × light/dark in tokens.css', () => {
    for (const theme of THEMES) {
      expect(tokensCss).toContain(`[data-theme='${theme}'][data-mode='light']`);
      expect(tokensCss).toContain(`[data-theme='${theme}'][data-mode='dark']`);
    }
  });

  it('expresses Tailwind --c-* color tokens as RGB triples, never hex', () => {
    // Every --c-<color> definition must be three space-separated integers.
    const defs = [...tokensCss.matchAll(/--c-[a-z-]+:\s*([^;]+);/g)];
    expect(defs.length).toBeGreaterThan(0);
    for (const [, value] of defs) {
      expect(value.trim()).toMatch(/^\d{1,3} \d{1,3} \d{1,3}$/);
    }
  });

  it('derives the kit full-color vars from the triples (no hard-coded hex color tokens)', () => {
    expect(tokensCss).toContain('--surface: rgb(var(--c-surface));');
    expect(tokensCss).toContain('--accent: rgb(var(--c-accent));');
    // The collision regression: a hex/triple --accent or --surface color token
    // here would shadow Tailwind's backing var again. Only the rgb(var(--c-*))
    // derivation form is allowed for these names.
    expect(tokensCss).not.toMatch(/--accent:\s*#/);
    expect(tokensCss).not.toMatch(/--surface:\s*#/);
    expect(tokensCss).not.toMatch(/--accent:\s*\d/);
    expect(tokensCss).not.toMatch(/--surface:\s*\d/);
  });

  it('points Tailwind at the --c-* backing vars (keys unchanged)', () => {
    for (const key of TAILWIND_COLORS) {
      expect(tailwindConfig).toContain(`rgb(var(--c-${key}) / <alpha-value>)`);
    }
    // The old un-prefixed backing vars must be gone (they were the collision).
    expect(tailwindConfig).not.toMatch(/rgb\(var\(--surface\)/);
    expect(tailwindConfig).not.toMatch(/rgb\(var\(--accent\)/);
  });

  it('keeps index.css free of color token definitions (collision source removed)', () => {
    expect(indexCss).not.toMatch(/--surface:/);
    expect(indexCss).not.toMatch(/--accent:/);
    expect(indexCss).not.toMatch(/--foreground:/);
  });
});
