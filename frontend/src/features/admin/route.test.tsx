/**
 * Auto-discovery contract for the admin slice (#88, ADR-0008 §3): the feature
 * exposes a `route` at `/admin` and a `navItem` pointing at it, so
 * `routes/discovery.ts` registers the screen by scanning — no edit to the shared
 * `routes/router.tsx`. Asserts the descriptors are well-formed and consistent.
 */
import { describe, it, expect } from 'vitest';
import { isValidElement } from 'react';
import { route } from './route';
import { navItem } from './nav';

describe('admin route/nav discovery descriptors', () => {
  it('exposes a /admin route with a renderable element', () => {
    expect(route.path).toBe('/admin');
    expect(isValidElement(route.element)).toBe(true);
  });

  it('contributes a nav item that targets the same path', () => {
    expect(navItem.to).toBe('/admin');
    expect(navItem.label).toBe('Admin');
    expect(navItem.to).toBe(route.path);
  });
});
