/**
 * GroupsPanel (#540, ADR-0022). Covers every state the panel can be in —
 * loading, empty, read error WITH a retry, the role-gated 403/401 dead end
 * WITHOUT one, and success — plus each write (create / rename / delete) and the
 * conflicts the server distinguishes by RFC-9457 `code`.
 *
 * Two assertions carry the model's meaning rather than its mechanics:
 *   - the system group's `member_count: null` renders as "Everyone in this
 *     tenant", never "0 members" — null is not zero (ADR-0022 §3);
 *   - the system row exposes NO rename / delete / member controls, so the
 *     client can't offer an action the server answers with 409.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api';
import type { Group, GroupList } from '@/api';
import { renderWithQuery } from '@/test/renderWithQuery';
import { GroupsPanel } from './GroupsPanel';

const listGroups = vi.hoisted(() => vi.fn());
const createGroup = vi.hoisted(() => vi.fn());
const renameGroup = vi.hoisted(() => vi.fn());
const deleteGroup = vi.hoisted(() => vi.fn());
const listGroupMembers = vi.hoisted(() => vi.fn());
const listMembers = vi.hoisted(() => vi.fn());
const addGroupMember = vi.hoisted(() => vi.fn());
const removeGroupMember = vi.hoisted(() => vi.fn());

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listGroups,
    createGroup,
    renameGroup,
    deleteGroup,
    listGroupMembers,
    listMembers,
    addGroupMember,
    removeGroupMember,
  };
});

const SYSTEM_GROUP: Group = {
  id: 'g-sys',
  name: 'All members',
  kind: 'system',
  member_count: null,
  created_at: '2026-07-30T00:00:00Z',
  updated_at: '2026-07-30T00:00:00Z',
};

const TAX_TEAM: Group = {
  id: 'g-tax',
  name: 'Tax Team',
  kind: 'user',
  member_count: 2,
  created_at: '2026-07-30T00:00:00Z',
  updated_at: '2026-07-30T00:00:00Z',
};

const GROUPS: GroupList = { items: [SYSTEM_GROUP, TAX_TEAM] };

beforeEach(() => {
  listGroups.mockReset().mockResolvedValue(GROUPS);
  createGroup.mockReset();
  renameGroup.mockReset();
  deleteGroup.mockReset();
  listGroupMembers.mockReset().mockResolvedValue({ items: [] });
  listMembers.mockReset().mockResolvedValue({ items: [], next_cursor: null });
  addGroupMember.mockReset();
  removeGroupMember.mockReset();
});

/** The table row for a group, so assertions can be scoped to it. */
function rowFor(name: string): HTMLElement {
  const cell = screen.getByText(name);
  const row = cell.closest('tr');
  if (row === null) throw new Error(`no row for ${name}`);
  return row;
}

describe('GroupsPanel', () => {
  it('shows a labelled loading state while the groups read is in flight', () => {
    listGroups.mockReturnValue(new Promise(() => {}));
    renderWithQuery(<GroupsPanel />);
    expect(screen.getByRole('status', { name: /loading groups/i })).toBeInTheDocument();
  });

  it('shows an empty state, with the create form still reachable', async () => {
    listGroups.mockResolvedValue({ items: [] });
    renderWithQuery(<GroupsPanel />);
    expect(await screen.findByText(/no groups yet/i)).toBeInTheDocument();
    // The form lives outside PanelBody precisely so it survives the empty state.
    expect(screen.getByRole('form', { name: /create group/i })).toBeInTheDocument();
  });

  it('surfaces a read failure as an actionable error and retries', async () => {
    listGroups.mockRejectedValueOnce(new ApiError('boom', 500));
    renderWithQuery(<GroupsPanel />);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/couldn’t load this panel/i)).toBeInTheDocument();

    listGroups.mockResolvedValueOnce(GROUPS);
    await userEvent.click(within(alert).getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('Tax Team')).toBeInTheDocument();
  });

  it('renders a non-admin 403 as an honest dead end with NO retry (INV-5)', async () => {
    listGroups.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupsPanel />);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/don’t have access/i)).toBeInTheDocument();
    expect(within(alert).getByText(/need the admin role/i)).toBeInTheDocument();
    // Retrying would only fetch the same refusal.
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders an expired session (401) as a dead end too (INV-4)', async () => {
    listGroups.mockRejectedValue(new ApiError('unauthorized', 401));
    renderWithQuery(<GroupsPanel />);

    expect(await screen.findByText(/session has expired/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('reads the system group’s null count as everyone, not zero', async () => {
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    const systemRow = rowFor('All members');
    expect(within(systemRow).getByText(/everyone in this tenant/i)).toBeInTheDocument();
    expect(within(systemRow).queryByText(/0 members/i)).not.toBeInTheDocument();
    expect(within(rowFor('Tax Team')).getByText('2 members')).toBeInTheDocument();
  });

  it('offers no rename, delete or member controls on the system group', async () => {
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    const systemRow = rowFor('All members');
    expect(within(systemRow).queryByRole('button')).not.toBeInTheDocument();
    expect(within(systemRow).getByText(/every member of this tenant, always/i)).toBeInTheDocument();

    // The user group carries all three.
    const taxRow = rowFor('Tax Team');
    expect(within(taxRow).getByRole('button', { name: /rename tax team/i })).toBeInTheDocument();
    expect(within(taxRow).getByRole('button', { name: /delete tax team/i })).toBeInTheDocument();
    expect(
      within(taxRow).getByRole('button', { name: /manage members of tax team/i }),
    ).toBeInTheDocument();
  });

  it('creates a group and reports it', async () => {
    const created: Group = { ...TAX_TEAM, id: 'g-new', name: 'Payroll', member_count: 0 };
    createGroup.mockResolvedValue(created);
    listGroups.mockResolvedValueOnce(GROUPS).mockResolvedValue({
      items: [SYSTEM_GROUP, TAX_TEAM, created],
    });

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.type(screen.getByLabelText(/^group name$/i), '  Payroll  ');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    // Trimmed before it reaches the wire.
    await waitFor(() => expect(createGroup).toHaveBeenCalledWith({ name: 'Payroll' }));
    expect(await screen.findByText(/created payroll/i)).toBeInTheDocument();
    expect(await screen.findByText('Payroll')).toBeInTheDocument();
  });

  it('maps a duplicate name (409 group_name_taken) to its own message', async () => {
    createGroup.mockRejectedValue(
      new ApiError('conflict', 409, {
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        code: 'group_name_taken',
      }),
    );
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.type(screen.getByLabelText(/^group name$/i), 'Tax Team');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/already exists/i)).toBeInTheDocument();
  });

  it('maps the reserved name (409 group_name_reserved) to its own message', async () => {
    createGroup.mockRejectedValue(
      new ApiError('conflict', 409, {
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        code: 'group_name_reserved',
      }),
    );
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.type(screen.getByLabelText(/^group name$/i), 'all members');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    expect(await screen.findByText(/reserved for the tenant-wide group/i)).toBeInTheDocument();
  });

  it('renames a group inline', async () => {
    const renamed: Group = { ...TAX_TEAM, name: 'Tax & Treasury' };
    renameGroup.mockResolvedValue(renamed);
    listGroups.mockResolvedValueOnce(GROUPS).mockResolvedValue({
      items: [SYSTEM_GROUP, renamed],
    });

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.click(screen.getByRole('button', { name: /rename tax team/i }));
    const field = screen.getByLabelText(/new name for tax team/i);
    await userEvent.clear(field);
    await userEvent.type(field, 'Tax & Treasury');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(renameGroup).toHaveBeenCalledWith('g-tax', { name: 'Tax & Treasury' }),
    );
    expect(await screen.findByText(/renamed to tax & treasury/i)).toBeInTheDocument();
  });

  it('confirms before deleting, then deletes', async () => {
    deleteGroup.mockResolvedValue(undefined);
    listGroups.mockResolvedValueOnce(GROUPS).mockResolvedValue({ items: [SYSTEM_GROUP] });

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.click(screen.getByRole('button', { name: /delete tax team/i }));
    const dialog = await screen.findByRole('alertdialog');
    // The confirm names the real consequence: shares go with the group.
    expect(within(dialog).getByText(/every share that names it/i)).toBeInTheDocument();
    expect(deleteGroup).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole('button', { name: /delete group/i }));
    await waitFor(() => expect(deleteGroup).toHaveBeenCalledWith('g-tax'));
    await waitFor(() => expect(screen.queryByText('Tax Team')).not.toBeInTheDocument());
  });

  it('keeps the dialog open and shows the failure when a delete is refused', async () => {
    deleteGroup.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.click(screen.getByRole('button', { name: /delete tax team/i }));
    const dialog = await screen.findByRole('alertdialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /delete group/i }));

    expect(await within(dialog).findByText(/need the admin role to manage groups/i)).toBeInTheDocument();
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText('Tax Team')).toBeInTheDocument();
  });

  it('opens the member roster for a user group only when asked', async () => {
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');
    expect(listGroupMembers).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /manage members of tax team/i }));
    expect(
      await screen.findByRole('heading', { name: /members of tax team/i }),
    ).toBeInTheDocument();
    await waitFor(() => expect(listGroupMembers).toHaveBeenCalled());
  });
});
