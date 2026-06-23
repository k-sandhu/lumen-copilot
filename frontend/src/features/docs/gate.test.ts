/**
 * Dev-page gate semantics (issue #40), proven without re-evaluating the route
 * modules (the flag is fixed per vitest process). Two facts make the OFF path
 * safe:
 *
 *   1. `parseBoolFlag` (the gate's truth source, api/env.ts) treats an unset /
 *      empty / "false" / "0" `VITE_ENABLE_DEV_PAGES` as OFF — so the production
 *      default (no env var) keeps the dev pages off.
 *   2. When OFF, each dev feature exports `route: undefined` / `navItem:
 *      undefined`. `routes/discovery.ts` registers a feature only when its export
 *      is a non-null OBJECT, so an `undefined` export is dropped from BOTH the
 *      router manifest and the nav overlay — `/docs` and `/features` become
 *      unroutable (404) and disappear from the nav, with no edit to the shared
 *      discovery/router seam.
 *
 * The discovery predicate is replicated here (it is not exported) to lock the
 * contract the gate relies on; `routes/discovery.test.ts` covers the live glob.
 * A final block re-imports the actual dev-feature modules with the flag forced
 * OFF (production env) and asserts they export `undefined` — the direct proof of
 * "flag off ⇒ /docs & /features absent from nav and not routable".
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
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

describe('flag OFF (production env) drops every dev route + nav export', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('docs & feature-catalog export undefined when the flag is off', async () => {
    // Production mode (so the test-mode default does not apply) with the flag
    // unset — exactly the production default. Re-import the modules fresh so the
    // module-level gate re-evaluates against the stubbed env.
    vi.resetModules();
    vi.stubEnv('MODE', 'production');
    vi.stubEnv('VITE_ENABLE_DEV_PAGES', '');

    const [docsRoute, docsNav, fcRoute, fcNav] = await Promise.all([
      import('./route'),
      import('./nav'),
      import('../feature-catalog/route'),
      import('../feature-catalog/nav'),
    ]);

    // Undefined exports ⇒ auto-discovery drops them ⇒ /docs & /features are
    // absent from the nav and unroutable (404).
    expect(docsRoute.route).toBeUndefined();
    expect(docsNav.navItem).toBeUndefined();
    expect(fcRoute.route).toBeUndefined();
    expect(fcNav.navItem).toBeUndefined();
  });

  it('the same modules register again when the flag is on', async () => {
    vi.resetModules();
    vi.stubEnv('MODE', 'production');
    vi.stubEnv('VITE_ENABLE_DEV_PAGES', 'true');

    const [docsRoute, fcRoute] = await Promise.all([
      import('./route'),
      import('../feature-catalog/route'),
    ]);

    expect(docsRoute.route?.path).toBe('/docs/*');
    expect(fcRoute.route?.path).toBe('/features');
  });
});
