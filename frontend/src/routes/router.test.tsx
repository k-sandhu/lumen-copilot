/**
 * App router regression coverage for issue #161: unknown URLs must render an
 * app-wide NotFoundPage inside RouteGuard -> AppShell, never a blank <Outlet/>.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearAccessToken, setAccessToken } from '@/api';
import { TooltipProvider } from '@/components/Tooltip';
import { RouteGuard, useAuthStore } from '@/features/auth';
import { NotFoundPage } from './NotFoundPage';
import { router } from './router';
import { AppShell } from './shell';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function pendingResponse(): Promise<Response> {
  return new Promise<Response>(() => {});
}

function apiResponse(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  if (url.includes('/auth/refresh')) return pendingResponse();
  if (url.includes('/auth/me')) return pendingResponse();
  return Promise.resolve(jsonResponse({ items: [], next_cursor: null }));
}

function renderRoutedShell(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route
              element={
                <RouteGuard>
                  <AppShell />
                </RouteGuard>
              }
            >
              <Route path="/" element={<div>assistant screen</div>} />
              <Route path="/search" element={<div>search screen</div>} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
  setAccessToken('jwt');
  useAuthStore.setState({ status: 'authenticated' });
  vi.spyOn(globalThis, 'fetch').mockImplementation(apiResponse);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
});

describe('app router catch-all', () => {
  it('registers the app-wide catch-all as the final layout child', () => {
    const layout = router.routes[0];
    const children = layout?.children ?? [];

    expect(children.at(-1)?.path).toBe('*');
  });

  it('renders the not-found state inside the authenticated shell for an unknown URL', () => {
    renderRoutedShell('/this-page-does-not-exist');

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('Page not found')).toBeInTheDocument();
    expect(screen.getByText('Lumen')).toBeInTheDocument();
    expect(screen.getByText('Copilot')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /primary/i })).toBeInTheDocument();
  });

  it('keeps known feature paths on their routed screen', () => {
    renderRoutedShell('/');

    expect(screen.getByText('assistant screen')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
