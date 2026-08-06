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
    // Scope to the roster LIST. A bare findByText('grace@…') would already be
    // satisfied by her <option> in the picker and would pass even if the
    // roster never re-read at all.
    const list = screen.getByRole('list', { name: /members of tax team/i });
    await waitFor(() => expect(within(list).getByText('grace@acme.test')).toBeInTheDocument());
    // …and she is no longer offered as a candidate, because she is now a member.
    const picker = screen.getByLabelText(/add member/i);
    expect(within(picker).queryByRole('option', { name: 'grace@acme.test' })).toBeNull();
    expect(await screen.findByText(/added grace@acme.test to tax team/i)).toBeInTheDocument();
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

  it('puts a failed removal on the row it belongs to, and only that row', async () => {
    // Two rows, so a failure sprayed onto every row cannot pass this.
    listGroupMembers.mockResolvedValue({ items: [ADA, GRACE] });
    removeGroupMember.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));

    const adaRow = (await screen.findByText('ada@acme.test')).closest('li') as HTMLElement;
    const graceRow = (await screen.findByText('grace@acme.test')).closest('li') as HTMLElement;
    expect(
      await within(adaRow).findByText(/need the admin role to manage groups/i),
    ).toBeInTheDocument();
    expect(within(graceRow).queryByRole('alert')).toBeNull();
  });

  it('announces a removal, since the button that had focus is destroyed by it', async () => {
    removeGroupMember.mockResolvedValue(undefined);
    listGroupMembers.mockResolvedValueOnce(MEMBERS).mockResolvedValue({ items: [] });
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));
    expect(
      await screen.findByText(/removed ada@acme.test from tax team/i),
    ).toBeInTheDocument();
    // Focus lands on the picker rather than falling to <body>.
    await waitFor(() => expect(screen.getByLabelText(/add member/i)).toHaveFocus());
  });

  it('says the roster is loading rather than silently disabling the picker', async () => {
    listMembers.mockReturnValue(new Promise(() => {}));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    expect(await screen.findByText(/loading this tenant’s members/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/add member/i)).toHaveAttribute('aria-busy', 'true');
  });

  it('offers no retry when the ROSTER read is refused either (403)', async () => {
    listMembers.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    expect(
      await screen.findByText(/need the admin role to list this tenant’s members/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
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

  it('walks the roster cursor so a member beyond the first page can be added', async () => {
    const LIN = {
      id: 'u3',
      email: 'lin@acme.test',
      role: ['member' as const],
      email_attested_at: null,
    };
    // Page 1 holds only people already in the group; Lin is on page 2.
    listMembers.mockImplementation((page: { cursor?: string } = {}) =>
      Promise.resolve(
        page.cursor === undefined
          ? { items: [ADA], next_cursor: 'page2' }
          : { items: [LIN], next_cursor: null },
      ),
    );
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    // With pages left it must NOT claim the tenant is fully enrolled.
    expect(screen.queryByText(/everyone in this tenant is already in this group/i)).toBeNull();
    expect(await screen.findByText(/more members are available/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /load more members/i }));

    const picker = await screen.findByLabelText(/add member/i);
    expect(await within(picker).findByRole('option', { name: 'lin@acme.test' })).toBeInTheDocument();

    addGroupMember.mockResolvedValue(undefined);
    await userEvent.selectOptions(picker, 'u3');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));
    await waitFor(() => expect(addGroupMember).toHaveBeenCalledWith('g-tax', { user_id: 'u3' }));
  });

  it('claims everyone is enrolled only once the cursor is exhausted', async () => {
    listMembers.mockResolvedValue({ items: [ADA], next_cursor: null });
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    expect(
      await screen.findByText(/everyone in this tenant is already in this group/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /load more members/i })).toBeNull();
  });

  it('withholds the add form when the group was deleted under us (404)', async () => {
    listGroupMembers.mockRejectedValue(new ApiError('gone', 404));
    renderWithQuery(<GroupMembersSection group={GROUP} />);

    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument();
    // Neither a retry nor a write could succeed against a deleted group.
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: /add a member/i })).toBeNull();
  });

  it('withholds the add form for a non-admin (403)', async () => {
    listGroupMembers.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<GroupMembersSection group={GROUP} />);

    await screen.findByText(/need the admin role to view this/i);
    expect(screen.queryByRole('form', { name: /add a member/i })).toBeNull();
  });

  it('keeps two genuinely overlapping removals from borrowing each other’s outcome', async () => {
    listGroupMembers.mockResolvedValue({ items: [ADA, GRACE] });
    // Deferred on purpose: both removals must be IN FLIGHT at once, or a shared
    // mutation observer would never get the chance to lose the first outcome.
    let rejectAda: (e: unknown) => void = () => {};
    let resolveGrace: () => void = () => {};
    removeGroupMember.mockImplementation((_g: string, userId: string) =>
      userId === 'u1'
        ? new Promise((_res, rej) => {
            rejectAda = rej;
          })
        : new Promise<void>((res) => {
            resolveGrace = () => res();
          }),
    );
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));
    await userEvent.click(screen.getByRole('button', { name: /remove grace@acme.test/i }));

    // Both pending simultaneously — each row shows its own busy label.
    const adaRow = screen.getByText('ada@acme.test').closest('li') as HTMLElement;
    const graceRow = screen.getByText('grace@acme.test').closest('li') as HTMLElement;
    expect(within(adaRow).getByRole('button', { name: /remove ada/i })).toHaveTextContent(
      /removing/i,
    );
    expect(within(graceRow).getByRole('button', { name: /remove grace/i })).toHaveTextContent(
      /removing/i,
    );

    // Settle out of order: the later call finishes first.
    resolveGrace();
    await waitFor(() =>
      expect(within(graceRow).getByRole('button', { name: /remove grace/i })).toHaveTextContent(
        /^remove$/i,
      ),
    );
    rejectAda(new ApiError('forbidden', 403));

    // Ada's failure must still arrive, on Ada's row only.
    expect(
      await within(adaRow).findByText(/need the admin role to manage groups/i),
    ).toBeInTheDocument();
    expect(within(graceRow).queryByRole('alert')).toBeNull();
  });

  it('withholds the picker while the group’s own membership is still unknown', async () => {
    // The roster resolves first; the group's members are still in flight, so
    // every candidate would look addable — including people already in it.
    listGroupMembers.mockReturnValue(new Promise(() => {}));
    renderWithQuery(<GroupMembersSection group={GROUP} />);

    const picker = await screen.findByLabelText(/add member/i);
    // Wait for the TENANT roster to land — options only exist once it has — so
    // `disabled` can no longer be explained by the roster still loading. The
    // only remaining reason is that this group's membership is unknown.
    await waitFor(() => expect(within(picker).getAllByRole('option').length).toBeGreaterThan(1));
    expect(picker).toBeDisabled();
    expect(await screen.findByText(/loading this group’s members/i)).toBeInTheDocument();
  });

  it('blocks member writes when the ROSTER read is refused, even with a page cached', async () => {
    // Page one loads, page two 403s: cached candidates are not permission.
    let call = 0;
    listMembers.mockImplementation(() => {
      call += 1;
      return call === 1
        ? Promise.resolve({ items: [GRACE], next_cursor: 'page2' })
        : Promise.reject(new ApiError('forbidden', 403));
    });
    renderWithQuery(<GroupMembersSection group={GROUP} />);
    await screen.findByText('ada@acme.test');

    await userEvent.click(await screen.findByRole('button', { name: /load more members/i }));

    expect(
      await screen.findByText(/need the admin role to list this tenant’s members/i),
    ).toBeInTheDocument();
    // The whole write surface goes, not just the retry.
    expect(screen.queryByRole('form', { name: /add a member/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^add$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
  });

  it('does not steal focus back if the admin moved on during a removal', async () => {
    listGroupMembers.mockResolvedValueOnce(MEMBERS).mockResolvedValue({ items: [] });
    let finish: () => void = () => {};
    removeGroupMember.mockImplementation(
      () =>
        new Promise<void>((res) => {
          finish = () => res();
        }),
    );
    renderWithQuery(
      <>
        <GroupMembersSection group={GROUP} />
        <button type="button">Somewhere else</button>
      </>,
    );
    await screen.findByText('ada@acme.test');

    await userEvent.click(screen.getByRole('button', { name: /remove ada@acme.test/i }));
    const elsewhere = screen.getByRole('button', { name: /somewhere else/i });
    elsewhere.focus();
    finish();

    await screen.findByText(/removed ada@acme.test/i);
    expect(elsewhere).toHaveFocus();
  });
});
