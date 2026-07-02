/**
 * Auto-discovery contract for the assistants slice (#212, ADR-0008 §3): the
 * feature exposes a splat `route` at `/assistants/*` (so it owns its three product
 * paths via a nested <Routes>) and a `navItem` pointing at `/assistants`, so
 * `routes/discovery.ts` registers the screen by scanning — no edit to the shared
 * `routes/router.tsx`. Asserts the descriptors are well-formed and consistent.
 */
import { describe, it, expect } from 'vitest';
import { isValidElement } from 'react';
import { route } from './route';
import { navItem } from './nav';

describe('assistants route/nav discovery descriptors', () => {
  it('exposes an /assistants/* splat route with a renderable element', () => {
    expect(route.path).toBe('/assistants/*');
    expect(isValidElement(route.element)).toBe(true);
  });

  it('contributes a nav item at the route root (the splat is stripped)', () => {
    expect(navItem.to).toBe('/assistants');
    expect(navItem.label).toBe('Assistants');
    // The nav target must match the route once the `/*` splat is stripped
    // (the same rule the discovery test enforces app-wide).
    expect(route.path.replace(/\/\*$/, '')).toBe(navItem.to);
  });
});
