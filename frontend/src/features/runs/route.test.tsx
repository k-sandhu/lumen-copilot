/**
 * Auto-discovery contract for the runs slice (#237, ADR-0008 §3): the run inbox +
 * detail surface is a distinct top-level path (`/runs/*`) from schedules, so it is
 * its own discovered route module (rendering the schedules slice's RunsPage). It
 * contributes a `navItem` at `/runs`.
 */
import { describe, it, expect } from 'vitest';
import { isValidElement } from 'react';
import { route } from './route';
import { navItem } from './nav';

describe('runs route/nav discovery descriptors', () => {
  it('exposes a /runs/* splat route with a renderable element', () => {
    expect(route.path).toBe('/runs/*');
    expect(isValidElement(route.element)).toBe(true);
  });

  it('contributes a nav item at the route root (the splat is stripped)', () => {
    expect(navItem.to).toBe('/runs');
    expect(navItem.label).toBe('Run history');
    expect(route.path.replace(/\/\*$/, '')).toBe(navItem.to);
  });
});
