/**
 * BrandingPanel state coverage: the per-tenant application-logo WRITE surface.
 * Covers the states that matter (frontend/AGENTS.md: not just success):
 *
 * - renders the current-logo summary + file picker + actions;
 * - shows "no custom logo" when /auth/me has no logo_url, and the current logo img
 *   when it does;
 * - selecting a valid file previews it and Upload calls updateTenantBranding(file);
 * - a rejected file type is refused CLIENT-SIDE without calling the API;
 * - Remove calls clearTenantBranding;
 * - a write error (403) surfaces a dismissible alert.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { CurrentUser } from '@/api';
import { BrandingPanel } from './BrandingPanel';

const getCurrentUser = vi.hoisted(() => vi.fn());
const hasAccessToken = vi.hoisted(() => vi.fn(() => true));
const updateTenantBranding = vi.hoisted(() => vi.fn());
const clearTenantBranding = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    getCurrentUser,
    hasAccessToken,
    updateTenantBranding,
    clearTenantBranding,
  };
});

const ME_NO_LOGO: CurrentUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'admin@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  tenant_name: 'Acme',
  roles: ['admin'],
  created_at: '2026-06-18T00:00:00Z',
  logo_url: null,
};

const ME_WITH_LOGO: CurrentUser = { ...ME_NO_LOGO, logo_url: 'https://storage.test/logo.png' };

function pngFile(name = 'logo.png'): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/png' });
}

beforeEach(() => {
  getCurrentUser.mockReset();
  updateTenantBranding.mockReset();
  clearTenantBranding.mockReset();
  hasAccessToken.mockReturnValue(true);
});

describe('BrandingPanel', () => {
  it('renders the current-logo summary, file picker, and actions', async () => {
    getCurrentUser.mockResolvedValue(ME_NO_LOGO);
    renderWithQuery(<BrandingPanel />);

    expect(await screen.findByText(/no custom logo/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/choose an image/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /upload logo/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /remove logo/i })).toBeInTheDocument();
  });

  it('shows the current logo image when the tenant has one set', async () => {
    getCurrentUser.mockResolvedValue(ME_WITH_LOGO);
    renderWithQuery(<BrandingPanel />);

    const img = await screen.findByTestId('current-logo');
    expect(img).toHaveAttribute('src', ME_WITH_LOGO.logo_url);
  });

  it('previews a selected file and Upload calls updateTenantBranding with the file', async () => {
    getCurrentUser.mockResolvedValue(ME_NO_LOGO);
    updateTenantBranding.mockResolvedValue({ logo_url: 'https://storage.test/new.png' });
    renderWithQuery(<BrandingPanel />);
    await screen.findByText(/no custom logo/i);

    const file = pngFile();
    await userEvent.upload(screen.getByLabelText(/choose an image/i), file);
    // The preview appears once a valid file is chosen.
    expect(await screen.findByTestId('logo-preview')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /upload logo/i }));
    expect(updateTenantBranding).toHaveBeenCalledWith(file);
    expect(await screen.findByText(/logo updated/i)).toBeInTheDocument();
  });

  it('refuses an unsupported file type client-side without calling the API', async () => {
    getCurrentUser.mockResolvedValue(ME_NO_LOGO);
    renderWithQuery(<BrandingPanel />);
    await screen.findByText(/no custom logo/i);

    // Fire a change with a non-image directly (bypassing the input's `accept`
    // filter userEvent honours) to exercise the component's own type guard —
    // the defense-in-depth path a drag-drop / programmatic set would take.
    const input = screen.getByLabelText(/choose an image/i);
    const bad = new File(['nope'], 'notes.txt', { type: 'text/plain' });
    fireEvent.change(input, { target: { files: [bad] } });

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/unsupported image type/i)).toBeInTheDocument();
    expect(updateTenantBranding).not.toHaveBeenCalled();
    // No preview for a rejected file.
    expect(screen.queryByTestId('logo-preview')).not.toBeInTheDocument();
  });

  it('Remove calls clearTenantBranding when a logo is set', async () => {
    getCurrentUser.mockResolvedValue(ME_WITH_LOGO);
    clearTenantBranding.mockResolvedValue(undefined);
    renderWithQuery(<BrandingPanel />);
    await screen.findByTestId('current-logo');

    await userEvent.click(screen.getByRole('button', { name: /remove logo/i }));
    expect(clearTenantBranding).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/logo removed/i)).toBeInTheDocument();
  });

  it('surfaces a write error (403) as a dismissible alert', async () => {
    getCurrentUser.mockResolvedValue(ME_NO_LOGO);
    updateTenantBranding.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<BrandingPanel />);
    await screen.findByText(/no custom logo/i);

    await userEvent.upload(screen.getByLabelText(/choose an image/i), pngFile());
    await userEvent.click(screen.getByRole('button', { name: /upload logo/i }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
