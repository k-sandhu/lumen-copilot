/**
 * Auto-discovery contract for the settings slice (ADR-0008 §3): the feature exposes
 * a `route` at `/settings` and a `navItem` pointing at it, so `routes/discovery.ts`
 * registers the screen by scanning — no edit to the shared `routes/router.tsx`.
 * Asserts the descriptors are well-formed and consistent.
 */
import { describe, it, expect } from 'vitest';
import { isValidElement } from 'react';
import { route } from './route';
import { navItem } from './nav';

describe('settings route/nav discovery descriptors', () => {
  it('exposes a /settings route with a renderable element', () => {
    expect(route.path).toBe('/settings');
    expect(isValidElement(route.element)).toBe(true);
  });

  it('contributes a nav item that targets the same path', () => {
    expect(navItem.to).toBe('/settings');
    expect(navItem.label).toBe('Settings');
    expect(navItem.to).toBe(route.path);
  });
});
