/**
 * AccountMenu coverage — the top-bar avatar + account popover.
 *
 * Covers the two user-settings additions:
 * - the avatar button renders the user's uploaded picture (`avatar_url`) when set,
 *   else initials derived from the email;
 * - the popover carries a Settings link to `/settings` (above Sign out).
 *
 * `@/features/auth` is mocked so the test drives `useCurrentUser` directly and
 * stubs the nested `CurrentUserMenu` (its own tests cover sign-out).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import type { CurrentUser } from '@/api';
import { AccountMenu } from './AccountMenu';

const useCurrentUser = vi.hoisted(() => vi.fn());
vi.mock('@/features/auth', () => ({
  useCurrentUser,
  CurrentUserMenu: () => <button type="button">Sign out</button>,
}));

const ME: CurrentUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'alice.smith@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  tenant_name: 'Acme',
  roles: ['member'],
  created_at: '2026-06-18T00:00:00Z',
  logo_url: null,
  avatar_url: null,
};

function renderMenu(ui: ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

beforeEach(() => useCurrentUser.mockReset());

describe('AccountMenu', () => {
  it('renders initials on the avatar when no avatar_url is set', () => {
    useCurrentUser.mockReturnValue({ data: ME, isLoading: false, isError: false });
    renderMenu(<AccountMenu />);
    // "alice.smith" → "AS"
    expect(screen.getByRole('button', { name: /account menu/i })).toHaveTextContent('AS');
  });

  it('renders the uploaded avatar image when avatar_url is set', () => {
    useCurrentUser.mockReturnValue({
      data: { ...ME, avatar_url: 'https://storage.test/me.png' },
      isLoading: false,
      isError: false,
    });
    renderMenu(<AccountMenu />);
    const trigger = screen.getByRole('button', { name: /account menu/i });
    const img = trigger.querySelector('img');
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute('src', 'https://storage.test/me.png');
  });

  it('shows a Settings link to /settings in the open popover', async () => {
    useCurrentUser.mockReturnValue({ data: ME, isLoading: false, isError: false });
    renderMenu(<AccountMenu />);

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));
    const link = screen.getByRole('menuitem', { name: /settings/i });
    expect(link).toHaveAttribute('href', '/settings');
  });
});
