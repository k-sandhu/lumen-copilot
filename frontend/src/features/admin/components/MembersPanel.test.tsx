/**
 * MembersPanel state coverage (#88; #455 ADR-0019 §2): loading skeleton,
 * success roster (email + role badges + attestation state), empty, and the
 * admin-only 403 (INV-5) actionable error with a working Retry. The panel's
 * ONE mutating control is the identity-attestation action (audited
 * server-side): the roster still exposes NO invite / role-edit / remove.
 * Attest coverage: per-member state (Attested date vs Unattested), the action
 * POSTs and re-reads, and a failed attest (403) renders INLINE on its row —
 * never a blank pane.
 *
 * The api/ boundary is mocked so the panel is tested against the contract
 * shape (Member: id, email, role[], email_attested_at) without real transport.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { Member, MemberList } from '@/api';
import { MembersPanel } from './MembersPanel';

const listMembers = vi.hoisted(() => vi.fn());
const attestMemberIdentity = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listMembers, attestMemberIdentity };
});

const ROSTER: MemberList = {
  items: [
    { id: 'u1', email: 'admin@acme.test', role: ['admin'], email_attested_at: '2026-07-01T09:00:00Z' },
    { id: 'u2', email: 'sec@acme.test', role: ['security', 'member'], email_attested_at: null },
    { id: 'u3', email: 'plain@acme.test', role: ['member'], email_attested_at: null },
  ],
  next_cursor: null,
};

beforeEach(() => {
  listMembers.mockReset();
  attestMemberIdentity.mockReset();
});

function rowFor(email: string): HTMLElement {
  const row = screen.getByText(email).closest('tr');
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

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
    const secRow = rowFor('sec@acme.test');
    // Both roles surface for a multi-role member.
    expect(within(secRow).getByText('Security')).toBeInTheDocument();
    expect(within(secRow).getByText('Member')).toBeInTheDocument();
  });

  it('shows each member’s attestation state (Attested with date vs Unattested)', async () => {
    listMembers.mockResolvedValue(ROSTER);
    renderWithQuery(<MembersPanel />);

    await screen.findByText('admin@acme.test');
    expect(within(rowFor('admin@acme.test')).getByText(/attested .*2026/i)).toBeInTheDocument();
    expect(within(rowFor('plain@acme.test')).getByText(/unattested/i)).toBeInTheDocument();
  });

  it('has NO invite / role-edit / remove controls — attest is the only mutation (ADR-0019 §2)', async () => {
    listMembers.mockResolvedValue(ROSTER);
    renderWithQuery(<MembersPanel />);
    await screen.findByText('admin@acme.test');
    // Every button is an attest affordance; nothing else mutates the roster.
    const buttons = screen.getAllByRole('button');
    expect(buttons.length).toBe(ROSTER.items.length);
    for (const b of buttons) expect(b).toHaveAccessibleName(/attest/i);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  });

  it('attests a member (POST) and re-renders the returned attestation state', async () => {
    const attested: Member = {
      id: 'u3',
      email: 'plain@acme.test',
      role: ['member'],
      email_attested_at: '2026-07-18T12:00:00Z',
    };
    // The mutation invalidates the roster query; the refetch must serve the
    // POST-attest state (the server is the source of truth).
    listMembers.mockResolvedValueOnce(ROSTER).mockResolvedValue({
      items: ROSTER.items.map((m) => (m.id === 'u3' ? attested : m)),
      next_cursor: null,
    });
    attestMemberIdentity.mockResolvedValue(attested);
    const user = userEvent.setup();
    renderWithQuery(<MembersPanel />);

    await screen.findByText('plain@acme.test');
    await user.click(
      within(rowFor('plain@acme.test')).getByRole('button', { name: /attest identity for plain/i }),
    );

    expect(attestMemberIdentity).toHaveBeenCalledWith('u3');
    await waitFor(() =>
      expect(within(rowFor('plain@acme.test')).getByText(/attested .*2026/i)).toBeInTheDocument(),
    );
  });

  it('offers Re-attest for an already-attested member (idempotent refresh)', async () => {
    listMembers.mockResolvedValue(ROSTER);
    renderWithQuery(<MembersPanel />);
    await screen.findByText('admin@acme.test');
    expect(
      within(rowFor('admin@acme.test')).getByRole('button', { name: /re-attest identity/i }),
    ).toBeInTheDocument();
  });

  it('renders a failed attest 403 INLINE on its row — never a blank pane (INV-5)', async () => {
    listMembers.mockResolvedValue(ROSTER);
    attestMemberIdentity.mockRejectedValue(new ApiError('forbidden', 403));
    const user = userEvent.setup();
    renderWithQuery(<MembersPanel />);

    await screen.findByText('plain@acme.test');
    await user.click(
      within(rowFor('plain@acme.test')).getByRole('button', { name: /attest identity for plain/i }),
    );

    const alert = await within(rowFor('plain@acme.test')).findByRole('alert');
    expect(alert).toHaveTextContent(/admin role/i);
    // The other rows are untouched.
    expect(within(rowFor('sec@acme.test')).queryByRole('alert')).not.toBeInTheDocument();
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
