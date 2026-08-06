/**
 * GroupMembersSection (#540, ADR-0022). Covers the roster's own states —
 * loading, empty, error with retry, the 403 dead end — and both writes, plus
 * the two things the picker must get right: it never offers someone who is
 * already in the group, and it says so honestly when the roster read fails
 * rather than rendering an empty picker that reads as "everyone is already in".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api';
import type { Group, GroupMemberList, MemberList } from '@/api';
import { renderWithQuery } from '@/test/renderWithQuery';
import { GroupMembersSection } from './GroupMembersSection';

const listGroupMembers = vi.hoisted(() => vi.fn());
const listMembers = vi.hoisted(() => vi.fn());
const addGroupMember = vi.hoisted(() => vi.fn());
const removeGroupMember = vi.hoisted(() => vi.fn());

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listGroupMembers, listMembers, addGroupMember, removeGroupMember };
});

const GROUP: Group = {
  id: 'g-tax',
  name: 'Tax Team',
  kind: 'user',
  member_count: 1,
  created_at: '2026-07-30T00:00:00Z',
  updated_at: '2026-07-30T00:00:00Z',
};

const ADA = { id: 'u1', email: 'ada@acme.test', role: ['member' as const], email_attested_at: null };
const GRACE = {
  id: 'u2',
  email: 'grace@acme.test',
  role: ['admin' as const],
  email_attested_at: null,
};

const MEMBERS: GroupMemberList = { items: [ADA] };
const ROSTER: MemberList = { items: [ADA, GRACE], next_cursor: null };

beforeEach(() => {
  listGroupMembers.mockReset().mockResolvedValue(MEMBERS);
  listMembers.mockReset().mockResolvedValue(ROSTER);
  addGroupMember.mockReset();
  removeGroupMember.mockReset();
});

describe('GroupMembersSection', () => {
  it('shows a labelled loading state for the roster read', () => {
    listGroupMembers.mockReturnValue(new Promise(() => {}));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    expect(
      screen.getByRole('status', { name: /loading members of tax team/i }),
    ).toBeInTheDocument();
  });

  it('states plainly that an empty group reaches nobody', async () => {
    listGroupMembers.mockResolvedValue({ items: [] });
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    expect(await screen.findByText(/reaches nobody until you add someone/i)).toBeInTheDocument();
  });

  it('surfaces a roster read failure with a retry', async () => {
    listGroupMembers.mockRejectedValueOnce(new ApiError('boom', 500));
    renderWithQuery(<GroupMembersSection group={GROUP} />);

    const alert = await screen.findByRole('alert');
    listGroupMembers.mockResolvedValueOnce(MEMBERS);
    await userEvent.click(within(alert).getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('ada@acme.test')).toBeInTheDocument();
  });

  it('renders a 403 on the roster as a dead end with no retry (INV-5)', async () => {
    listGroupMembers.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupMembersSection group={GROUP} />);

    expect(await screen.findByText(/need the admin role to view this/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('offers only people who are not already in the group', async () => {
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    const picker = screen.getByLabelText(/add member/i);
    expect(within(picker).getByRole('option', { name: 'grace@acme.test' })).toBeInTheDocument();
    expect(within(picker).queryByRole('option', { name: 'ada@acme.test' })).not.toBeInTheDocument();
  });

  it('adds the chosen member and refreshes the roster', async () => {
    addGroupMember.mockResolvedValue(undefined);
    listGroupMembers.mockResolvedValueOnce(MEMBERS).mockResolvedValue({ items: [ADA, GRACE] });

    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.selectOptions(screen.getByLabelText(/add member/i), 'u2');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));

    await waitFor(() => expect(addGroupMember).toHaveBeenCalledWith('g-tax', { user_id: 'u2' }));
    expect(await screen.findByText('grace@acme.test')).toBeInTheDocument();
  });

  it('reports an add failure inline instead of discarding it', async () => {
    addGroupMember.mockRejectedValue(new ApiError('gone', 404));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.selectOptions(screen.getByLabelText(/add member/i), 'u2');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));

    expect(
      await screen.findByText(/group or user no longer exists in this tenant/i),
    ).toBeInTheDocument();
  });

  it('removes a member and refreshes the roster', async () => {
    removeGroupMember.mockResolvedValue(undefined);
    listGroupMembers.mockResolvedValueOnce(MEMBERS).mockResolvedValue({ items: [] });

    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));
    await waitFor(() => expect(removeGroupMember).toHaveBeenCalledWith('g-tax', 'u1'));
    expect(await screen.findByText(/reaches nobody until you add someone/i)).toBeInTheDocument();
  });

  it('puts a failed removal on the row it belongs to', async () => {
    removeGroupMember.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));
    const row = (await screen.findByText('ada@acme.test')).closest('li');
    expect(row).not.toBeNull();
    expect(
      await within(row as HTMLElement).findByText(/need the admin role to manage groups/i),
    ).toBeInTheDocument();
  });

  it('says the roster is unavailable rather than showing an empty picker', async () => {
    listMembers.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    expect(
      await screen.findByText(/couldn’t load the member roster/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/everyone in this tenant is already in this group/i)).toBeNull();
  });

  it('admits when the picker can only offer the first page of the roster', async () => {
    listMembers.mockResolvedValue({ items: [ADA, GRACE], next_cursor: 'more' });
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    expect(await screen.findByText(/only the first page/i)).toBeInTheDocument();
  });
});
