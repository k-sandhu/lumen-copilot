/**
 * CurrentUserMenu (AC-2): GET /auth/me populates the current-user UI; a logout
 * control calls POST /auth/logout and clears state. Covers loading + loaded +
 * the logout interaction.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { CurrentUserMenu } from './CurrentUserMenu';
import { useAuthStore } from '../model/authStore';
import { setAccessToken, clearAccessToken, getAccessToken } from '@/api';

const ME = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'kw@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  roles: ['member'],
  created_at: '2026-06-18T00:00:00Z',
};

function meResponse(): Response {
  return new Response(JSON.stringify(ME), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function noContent(): Response {
  return new Response(null, { status: 204 });
}

beforeEach(() => {
  setAccessToken('jwt');
  useAuthStore.setState({ status: 'authenticated' });
});
afterEach(() => {
  vi.restoreAllMocks();
  clearAccessToken();
});

describe('CurrentUserMenu', () => {
  it('shows the signed-in user from GET /auth/me (AC-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(meResponse());
    renderWithQuery(<CurrentUserMenu />);
    expect(await screen.findByText('kw@acme.test')).toBeInTheDocument();
  });

  it('logs out: calls POST /auth/logout and clears the token (AC-2)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/auth/logout')) return noContent();
      return meResponse();
    });
    const user = userEvent.setup();
    renderWithQuery(<CurrentUserMenu />);

    await screen.findByText('kw@acme.test');
    await user.click(screen.getByRole('button', { name: /sign out/i }));

    await waitFor(() => expect(getAccessToken()).toBeNull());
    expect(useAuthStore.getState().status).toBe('unauthenticated');

    const logoutCall = fetchSpy.mock.calls.find((c) => String(c[0]).includes('/auth/logout'));
    expect(logoutCall).toBeDefined();
    expect((logoutCall?.[1] as RequestInit).method).toBe('POST');
  });
});
