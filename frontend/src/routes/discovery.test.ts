/**
 * Smoke test for route + nav auto-discovery (ADR-0008 §3, issue #79).
 *
 * Proves the invariant that retires the `routes/router.tsx` append-target: the
 * route manifest is assembled by SCANNING the per-feature route modules via
 * `import.meta.glob`, and resolves EVERY existing feature route with the exact
 * paths the hand-edited array used to declare. Same for the nav overlay, which is
 * assembled from each feature's `nav.ts`.
 */
import { describe, expect, it } from 'vitest';
import { featureNavItems, featureRoutes } from './discovery';

// The four routes that existed before the refactor — discovery must resolve all
// of them, with identical paths (behavior-identical to the old manual array).
const EXPECTED_PATHS = ['/', '/docs/*', '/features', '/documents'];

// The three nav-overlay links that existed before the refactor.
const EXPECTED_NAV_TARGETS = ['/documents', '/docs', '/features'];

describe('route auto-discovery (import.meta.glob)', () => {
  it('resolves every existing feature route with its exact path', () => {
    const paths = featureRoutes.map((r) => r.path);
    for (const expected of EXPECTED_PATHS) {
      expect(paths).toContain(expected);
    }
  });

  it('gives every route a renderable element', () => {
    for (const route of featureRoutes) {
      // A valid React element is an object with a `type` — proves the glob
      // imported a real route.tsx, not an empty/partial module.
      expect(route.element).toBeTruthy();
      expect(typeof route.element).toBe('object');
    }
  });

  it('produces no duplicate route paths', () => {
    const paths = featureRoutes.map((r) => r.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('is deterministically ordered (by order then path)', () => {
    const sorted = [...featureRoutes].sort((a, b) => {
      const delta = (a.order ?? 0) - (b.order ?? 0);
      return delta !== 0 ? delta : a.path.localeCompare(b.path);
    });
    expect(featureRoutes.map((r) => r.path)).toEqual(sorted.map((r) => r.path));
  });

  it('keeps the chat shell at the index route', () => {
    expect(featureRoutes[0]?.path).toBe('/');
  });
});

describe('nav auto-discovery (import.meta.glob)', () => {
  it('resolves every existing nav overlay target', () => {
    const targets = featureNavItems.map((n) => n.to);
    for (const expected of EXPECTED_NAV_TARGETS) {
      expect(targets).toContain(expected);
    }
  });

  it('every nav target matches a discovered route', () => {
    // A nav link must point at a real route — `/docs` is covered by the `/docs/*`
    // splat, so compare on the leading segment.
    const routeRoots = new Set(featureRoutes.map((r) => r.path.replace(/\/\*$/, '')));
    for (const item of featureNavItems) {
      expect(routeRoots.has(item.to)).toBe(true);
    }
  });
});
