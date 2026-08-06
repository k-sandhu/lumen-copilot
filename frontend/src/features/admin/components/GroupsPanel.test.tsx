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

  it('keeps the typed name when creation is rejected, so it can be corrected', async () => {
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

    const field = screen.getByLabelText(/^group name$/i);
    await userEvent.type(field, 'Tax Team');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    await screen.findByText(/already exists/i);
    expect(field).toHaveValue('Tax Team');
  });

  it('clears the field only after a create actually succeeds', async () => {
    createGroup.mockResolvedValue({ ...TAX_TEAM, id: 'g-new', name: 'Payroll', member_count: 0 });
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    const field = screen.getByLabelText(/^group name$/i);
    await userEvent.type(field, 'Payroll');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    await screen.findByText(/created payroll/i);
    expect(field).toHaveValue('');
  });

  it('offers no create form when the read was refused (403)', async () => {
    listGroups.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupsPanel />);

    await screen.findByText(/need the admin role/i);
    // A write that could only be refused is the same dead end as the retry.
    expect(screen.queryByRole('form', { name: /create group/i })).toBeNull();
    expect(screen.queryByLabelText(/^group name$/i)).toBeNull();
  });

  it('keeps the create form on an ordinary read failure, which a write may survive', async () => {
    listGroups.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<GroupsPanel />);

    await screen.findByText(/couldn’t load this panel/i);
    expect(screen.getByRole('form', { name: /create group/i })).toBeInTheDocument();
  });

  it('treats a delete that 404s as the outcome the admin wanted', async () => {
    deleteGroup.mockRejectedValue(new ApiError('gone', 404));
    listGroups.mockResolvedValueOnce(GROUPS).mockResolvedValue({ items: [SYSTEM_GROUP] });

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.click(screen.getByRole('button', { name: /delete tax team/i }));
    const dialog = await screen.findByRole('alertdialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /delete group/i }));

    // Already gone is done, not a failure to act on: dialog closes…
    expect(await screen.findByText(/was already deleted/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
    // …and the phantom row goes with it, rather than sitting there clickable.
    // Asserting the toast alone would pass with no reconciliation at all.
    await waitFor(() => expect(screen.queryByText('Tax Team')).not.toBeInTheDocument());
    expect(listGroups.mock.calls.length).toBeGreaterThan(1);
  });

  it('drops the row immediately, without waiting on the refetch', async () => {
    deleteGroup.mockResolvedValue(undefined);
    // The refetch never resolves: the row must still go, from the cache write.
    listGroups.mockResolvedValueOnce(GROUPS).mockReturnValue(new Promise(() => {}));

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    await userEvent.click(screen.getByRole('button', { name: /delete tax team/i }));
    const dialog = await screen.findByRole('alertdialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /delete group/i }));

    await waitFor(() => expect(screen.queryByText('Tax Team')).not.toBeInTheDocument());
  });

  it('does not wipe a newer draft when an earlier create succeeds', async () => {
    let finish: (g: Group) => void = () => {};
    createGroup.mockImplementation(
      () =>
        new Promise<Group>((resolve) => {
          finish = resolve;
        }),
    );
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    const field = screen.getByLabelText(/^group name$/i);
    await userEvent.type(field, 'Payroll');
    await userEvent.click(screen.getByRole('button', { name: /create group/i }));

    // The field stays editable while the request is in flight.
    await userEvent.clear(field);
    await userEvent.type(field, 'Accounting');

    finish({ ...TAX_TEAM, id: 'g-new', name: 'Payroll', member_count: 0 });
    await screen.findByText(/created payroll/i);

    // Clearing on success must not discard what the admin has since typed.
    expect(field).toHaveValue('Accounting');
  });

  it('does not let a slower rename close a draft opened on another row', async () => {
    const OTHER: Group = { ...TAX_TEAM, id: 'g-fin', name: 'Finance' };
    listGroups.mockResolvedValue({ items: [SYSTEM_GROUP, TAX_TEAM, OTHER] });
    let settleTax: (g: Group) => void = () => {};
    renameGroup.mockImplementation(
      () => new Promise<Group>((resolve) => { settleTax = resolve; }),
    );

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Finance');

    // Start renaming Tax Team, leave it in flight, then open Finance's draft.
    await userEvent.click(screen.getByRole('button', { name: /rename tax team/i }));
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await userEvent.click(screen.getByRole('button', { name: /rename finance/i }));
    expect(screen.getByLabelText(/new name for finance/i)).toBeInTheDocument();

    settleTax({ ...TAX_TEAM, name: 'Tax Team' });

    // Tax's completion must not discard the draft the admin is now typing in.
    await screen.findByText(/renamed to tax team/i);
    expect(screen.getByLabelText(/new name for finance/i)).toBeInTheDocument();
  });

  it('returns focus to Rename when the inline form closes, rather than dropping it', async () => {
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Tax Team');

    const renameButton = screen.getByRole('button', { name: /rename tax team/i });
    await userEvent.click(renameButton);
    expect(screen.getByLabelText(/new name for tax team/i)).toHaveFocus();

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
    // Without a managed return, focus would fall to <body> when the form unmounts.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /rename tax team/i })).toHaveFocus(),
    );
  });

  it('does not steal focus back when the admin opens another row’s editor', async () => {
    const OTHER: Group = { ...TAX_TEAM, id: 'g-fin', name: 'Finance' };
    listGroups.mockResolvedValue({ items: [SYSTEM_GROUP, TAX_TEAM, OTHER] });
    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Finance');

    await userEvent.click(screen.getByRole('button', { name: /rename tax team/i }));
    // Switching editors closes Tax's form, but Finance's input now owns focus.
    await userEvent.click(screen.getByRole('button', { name: /rename finance/i }));

    await waitFor(() => expect(screen.getByLabelText(/new name for finance/i)).toHaveFocus());
  });

  it('does not carry one group’s picker choice across to another group', async () => {
    const OTHER: Group = { ...TAX_TEAM, id: 'g-fin', name: 'Finance' };
    listGroups.mockResolvedValue({ items: [SYSTEM_GROUP, TAX_TEAM, OTHER] });
    listGroupMembers.mockResolvedValue({ items: [] });
    listMembers.mockResolvedValue({
      items: [{ id: 'u9', email: 'ada@acme.test', role: ['member'], email_attested_at: null }],
      next_cursor: null,
    });

    renderWithQuery(<GroupsPanel />);
    await screen.findByText('Finance');

    await userEvent.click(screen.getByRole('button', { name: /manage members of tax team/i }));
    const taxPicker = await screen.findByLabelText(/add member/i);
    await userEvent.selectOptions(taxPicker, 'u9');
    expect(taxPicker).toHaveValue('u9');

    // Switching selection must remount the section, not re-prop it: a choice
    // made for Tax Team could otherwise be submitted against Finance.
    await userEvent.click(screen.getByRole('button', { name: /manage members of finance/i }));
    const finPicker = await screen.findByLabelText(/add member/i);
    expect(finPicker).toHaveValue('');
    expect(await screen.findByRole('heading', { name: /members of finance/i })).toBeInTheDocument();
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
