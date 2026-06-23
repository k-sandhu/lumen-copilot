/**
 * Dev-page gating contract for the docs slice (issue #40).
 *
 * `/docs` is a developer-only page (the in-app viewer that renders every repo
 * markdown file — ADRs, specs, research). It must be gated behind
 * `VITE_ENABLE_DEV_PAGES` + auth so it never leaks internal material in
 * production (AGENTS.md §2/§4, ADR-0007 excludes it from the product IA).
 *
 * The vitest env runs with the flag ON (api/env.ts defaults `MODE==='test'` to
 * true — env files are git-ignored, so there's no committed `.env.test`), so
 * here we assert the ON path: the route registers at `/docs/*`, its nav link is
 * present, and the rendered element is wrapped in the auth `RouteGuard` (the same
 * gate as `/documents`) so an unauthenticated visitor is sent to login, never the
 * docs. The OFF path (flag false ⇒ `route`/`navItem` are `undefined`, which
 * auto-discovery drops) is covered by `api/env.test.ts` against the pure flag
 * parser + the discovery filter semantics.
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

describe('docs dev-page route/nav (flag ON)', () => {
  it('registers a /docs/* route with a renderable element', () => {
    expect(route?.path).toBe('/docs/*');
    expect(isValidElement(route?.element)).toBe(true);
  });

  it('contributes a nav item targeting /docs', () => {
    expect(navItem?.to).toBe('/docs');
    expect(navItem?.label).toBe('Documentation');
  });

  it('gates the page behind auth — an unauthenticated visitor never sees the docs', async () => {
    // 401 on the silent-refresh probe ⇒ the RouteGuard resolves to the login
    // screen, so the lazy DocsPage is never rendered.
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
