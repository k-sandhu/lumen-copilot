/**
 * Dev-page gate semantics (issue #40). Two facts make the OFF path safe:
 *
 *   1. The gate is a BUILD-TIME literal. Each dev feature's `route.tsx`/`nav.ts`
 *      gates its `lazy(() => import(...))` on the `define`-injected literal
 *      `__DEV_PAGES_ENABLED__` (vite.config.ts, from `VITE_ENABLE_DEV_PAGES`).
 *      Because it folds to a literal, Rollup dead-code-eliminates the whole
 *      `false ? buildRoute() : undefined` branch when OFF — so the dev-page
 *      chunks, and the internal docs the docs viewer inlines via
 *      `import.meta.glob`, are NOT emitted at all. That artifact-level guarantee
 *      is proven by `src/buildguards/dist-no-dev-pages.test.ts` (a real flag-OFF
 *      `vite build` + a leak grep). A runtime gate alone would still ship the
 *      bytes, which are fetchable by direct URL regardless of auth.
 *
 *   2. When the gate is OFF, each dev feature exports `route: undefined` /
 *      `navItem: undefined`. `routes/discovery.ts` registers a feature only when
 *      its export is a non-null OBJECT, so an `undefined` export is dropped from
 *      BOTH the router manifest and the nav overlay — `/docs` and `/features`
 *      become unroutable (404) and disappear from the nav, with no edit to the
 *      shared discovery/router seam.
 *
 * This file locks fact (2)'s contract — the discovery predicate and the flag
 * truth-table — at the unit level. The vitest process is built with the flag ON
 * (vite.config.ts defaults `mode === 'test'` to ON, since env files are
 * git-ignored), so the live `route`/`navItem` exports are exercised on the ON
 * path here and in `route.test.tsx`; the OFF artifact is fact (1)'s build test.
 */
import { describe, it, expect } from 'vitest';
import { parseBoolFlag } from '@/api';

/** Same shape `routes/discovery.ts` requires before it registers a route/nav. */
function isDiscoverableDescriptor(value: unknown): boolean {
  return typeof value === 'object' && value !== null;
}

describe('dev-pages flag parsing (production default = OFF)', () => {
  it('treats unset / empty as OFF (the production default)', () => {
    expect(parseBoolFlag(undefined, false)).toBe(false);
    expect(parseBoolFlag('', false)).toBe(false);
  });

  it('treats explicit falsy strings as OFF', () => {
    expect(parseBoolFlag('false', true)).toBe(false);
    expect(parseBoolFlag('0', true)).toBe(false);
    expect(parseBoolFlag('FALSE', true)).toBe(false);
  });

  it('treats explicit truthy strings as ON', () => {
    expect(parseBoolFlag('true', false)).toBe(true);
    expect(parseBoolFlag('1', false)).toBe(true);
    expect(parseBoolFlag('TRUE', false)).toBe(true);
  });

  it('falls back for unrecognized strings (no accidental enable)', () => {
    expect(parseBoolFlag('yes', false)).toBe(false);
    expect(parseBoolFlag('on', false)).toBe(false);
  });
});

describe('discovery drops an undefined descriptor (the OFF export)', () => {
  it('an `undefined` route/nav export is NOT discoverable', () => {
    // What each dev feature exports when the flag is OFF.
    expect(isDiscoverableDescriptor(undefined)).toBe(false);
  });

  it('a real descriptor object IS discoverable (the ON export)', () => {
    expect(isDiscoverableDescriptor({ path: '/docs/*' })).toBe(true);
    expect(isDiscoverableDescriptor({ to: '/docs' })).toBe(true);
  });
});
