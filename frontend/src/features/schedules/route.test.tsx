/**
 * Auto-discovery contract for the schedules slice (#237, ADR-0008 §3): the feature
 * exposes a splat `route` at `/schedules/*` (so it owns its product paths via a
 * nested <Routes>) and a `navItem` pointing at `/schedules`, so
 * `routes/discovery.ts` registers the screen by scanning — no edit to the shared
 * `routes/router.tsx`.
 */
import { describe, it, expect } from 'vitest';
import { isValidElement } from 'react';
import { route } from './route';
import { navItem } from './nav';

describe('schedules route/nav discovery descriptors', () => {
  it('exposes a /schedules/* splat route with a renderable element', () => {
    expect(route.path).toBe('/schedules/*');
    expect(isValidElement(route.element)).toBe(true);
  });

  it('contributes a nav item at the route root (the splat is stripped)', () => {
    expect(navItem.to).toBe('/schedules');
    expect(navItem.label).toBe('Schedules');
    expect(route.path.replace(/\/\*$/, '')).toBe(navItem.to);
  });
});
