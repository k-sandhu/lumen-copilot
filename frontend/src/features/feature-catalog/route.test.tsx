/**
 * Dev-page gating contract for the feature-catalog slice (issue #40).
 *
 * `/features` is a developer-only page (the shipped-capabilities catalog, which
 * links into internal docs/ADRs/PRs). It must be gated behind
 * `VITE_ENABLE_DEV_PAGES` + auth so it never leaks internal material in
 * production (AGENTS.md §2/§4, ADR-0007 excludes it from the product IA).
 *
 * The vitest env runs with the flag ON (api/env.ts defaults `MODE==='test'` to
 * true), so here we assert the ON path: the route registers at `/features`, its
 * nav link is present, and the rendered element is wrapped in the auth
 * `RouteGuard` (the same gate as `/documents`) so an unauthenticated visitor is
 * sent to login, never the catalog. The OFF path (flag false ⇒ `route`/`navItem`
 * are `undefined`, dropped by auto-discovery) is covered by `api/env.test.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { isValidElement } from 'react';
import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { clearAccessToken } from '@/api';
import { useAuthStore } from '@/features/auth';
import { route } from './route';
import { navItem } from './nav';

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
});
afterEach(() => vi.restoreAllMocks());

describe('feature-catalog dev-page route/nav (flag ON)', () => {
  it('registers a /features route with a renderable element', () => {
    expect(route?.path).toBe('/features');
    expect(isValidElement(route?.element)).toBe(true);
  });

  it('contributes a nav item targeting /features', () => {
    expect(navItem?.to).toBe('/features');
    expect(navItem?.label).toBe('Features built');
  });

  it('gates the page behind auth — an unauthenticated visitor never sees the catalog', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 401 }), {
        status: 401,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    );

    renderWithQuery(<MemoryRouter>{route?.element}</MemoryRouter>);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument(),
    );
  });
});
