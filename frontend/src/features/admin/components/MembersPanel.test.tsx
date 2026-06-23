/**
 * MembersPanel state coverage (#88): loading skeleton, success roster (email +
 * role badges), empty, and the admin-only 403 (INV-5) actionable error with a
 * working Retry. Also asserts the read-only invariant (ADR-0007 §4): the roster
 * exposes NO mutating controls — no invite / role-edit / remove buttons.
 *
 * The api/ boundary is mocked so the panel is tested against the contract shape
 * (Member: id, email, role[]) without real transport.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { MemberList } from '@/api';
import { MembersPanel } from './MembersPanel';

const listMembers = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listMembers };
});

const ROSTER: MemberList = {
  items: [
    { id: 'u1', email: 'admin@acme.test', role: ['admin'] },
    { id: 'u2', email: 'sec@acme.test', role: ['security', 'member'] },
    { id: 'u3', email: 'plain@acme.test', role: ['member'] },
  ],
  next_cursor: null,
};

beforeEach(() => listMembers.mockReset());

describe('MembersPanel', () => {
  it('renders a loading state while the roster is in flight', () => {
    listMembers.mockReturnValue(new Promise(() => {}));
    renderWithQuery(<MembersPanel />);
    expect(screen.getByRole('status', { name: /loading members/i })).toBeInTheDocument();
  });

  it('renders members with their email and role badges', async () => {
    listMembers.mockResolvedValue(ROSTER);
    renderWithQuery(<MembersPanel />);

    expect(await screen.findByText('admin@acme.test')).toBeInTheDocument();
    const secRow = screen.getByText('sec@acme.test').closest('tr');
    expect(secRow).not.toBeNull();
    // Both roles surface for a multi-role member.
    expect(within(secRow as HTMLElement).getByText('Security')).toBeInTheDocument();
    expect(within(secRow as HTMLElement).getByText('Member')).toBeInTheDocument();
  });

  it('has NO mutating controls — read-only roster (ADR-0007 §4)', async () => {
    listMembers.mockResolvedValue(ROSTER);
    renderWithQuery(<MembersPanel />);
    await screen.findByText('admin@acme.test');
    // No invite/edit/remove affordances of any kind.
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  });

  it('shows an empty state when the tenant has no members', async () => {
    listMembers.mockResolvedValue({ items: [], next_cursor: null });
    renderWithQuery(<MembersPanel />);
    expect(await screen.findByText(/no members in this tenant/i)).toBeInTheDocument();
  });

  it('surfaces a non-admin 403 as an actionable error and retries (INV-5)', async () => {
    listMembers.mockRejectedValueOnce(new ApiError('forbidden', 403));
    renderWithQuery(<MembersPanel />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/admin role/i);

    // Retry refetches; on success the roster replaces the error.
    listMembers.mockResolvedValueOnce(ROSTER);
    await userEvent.click(within(alert).getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.getByText('admin@acme.test')).toBeInTheDocument());
  });
});
