/**
 * Brand cell — renders the tenant's application logo (admin branding) when one is
 * set on the principal (GET /auth/me `logo_url`), else the default sparkle mark +
 * "Lumen / Copilot" wordmark. The wordmark is always present so the brand cell is
 * never blank while the principal loads or when no logo is set.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement } from 'react';
import type { CurrentUser } from '@/api';
import { Brand } from './Brand';

const getCurrentUser = vi.hoisted(() => vi.fn());
const hasAccessToken = vi.hoisted(() => vi.fn(() => true));
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getCurrentUser, hasAccessToken };
});

const ME: CurrentUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'admin@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  tenant_name: 'Acme',
  roles: ['admin'],
  created_at: '2026-06-18T00:00:00Z',
  logo_url: null,
};

function renderBrand(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getCurrentUser.mockReset();
  hasAccessToken.mockReturnValue(true);
});

describe('Brand', () => {
  it('renders the default wordmark and no logo image when no tenant logo is set', async () => {
    getCurrentUser.mockResolvedValue(ME);
    renderBrand(<Brand />);

    expect(await screen.findByText('Lumen')).toBeInTheDocument();
    expect(screen.getByText('Copilot')).toBeInTheDocument();
    // No <img> — the default sparkle icon renders instead.
    expect(document.querySelector('.lc-brand__logo-img')).toBeNull();
  });

  it('renders the tenant logo image when logo_url is present', async () => {
    getCurrentUser.mockResolvedValue({ ...ME, logo_url: 'https://storage.test/logo.png' });
    renderBrand(<Brand />);

    // The wordmark still shows; the custom logo replaces the default mark.
    expect(await screen.findByText('Lumen')).toBeInTheDocument();
    const img = document.querySelector<HTMLImageElement>('.lc-brand__logo-img');
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute('src', 'https://storage.test/logo.png');
  });
});
