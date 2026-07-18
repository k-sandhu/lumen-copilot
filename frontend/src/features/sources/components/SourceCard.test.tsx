/**
 * SourceCard (#27, #455) — one connector card. Asserts the trust signals render
 * (status dot label, freshness, permission pill, indexed count), that a long
 * URL is set up to truncate (not break layout), and that per-source actions
 * are real keyboard-reachable buttons wired to their callbacks. A syncing card
 * disables its sync button so a second sync can't be double-fired.
 *
 * Managed (`gdrive`) coverage (ADR-0019 §5 health surface): connected-account
 * email, ACL freshness (`acl_synced_at`), `unmapped_acl_count` + the
 * attestation hint, the Reauthorize action when `reauthorize_required`, the
 * Connect action on `pending_auth` — and the INV-5 negative: a NON-admin sees
 * no managed-source affordances at all (no sync / remove / connect buttons).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api';
import type { GdriveSource, WebSource } from '@/api';
import { SourceCard } from './SourceCard';

function makeSource(overrides: Partial<WebSource> = {}): WebSource {
  return {
    id: 's1',
    type: 'web',
    config: { url: 'https://handbook.acme.com/policy', mode: 'page' },
    status: 'ready',
    indexed_count: 1284,
    last_synced_at: '2026-06-23T11:50:00Z',
    owner_id: 'u1',
    created_at: '2026-06-23T10:00:00Z',
    updated_at: '2026-06-23T11:50:00Z',
    ...overrides,
  };
}

function makeGdrive(overrides: Partial<GdriveSource> = {}): GdriveSource {
  return {
    id: 'g1',
    type: 'gdrive',
    config: { mode: 'my_drive' },
    status: 'ready',
    indexed_count: 240,
    last_synced_at: '2026-06-23T11:50:00Z',
    connected_account: { email: 'drive-ops@acme.com' },
    acl_synced_at: '2026-06-23T11:50:00Z',
    unmapped_acl_count: 0,
    reauthorize_required: false,
    owner_id: 'u1',
    created_at: '2026-06-23T10:00:00Z',
    updated_at: '2026-06-23T11:50:00Z',
    ...overrides,
  };
}

describe('SourceCard — web', () => {
  it('shows the name, status, freshness, permission and indexed count', () => {
    render(<SourceCard source={makeSource()} onSync={() => {}} onRemove={() => {}} />);
    const card = screen.getByRole('article', { name: /handbook\.acme\.com/i });
    expect(within(card).getByRole('heading', { name: 'handbook.acme.com' })).toBeInTheDocument();
    // The sync-health StatusDot label and the FreshnessPill both mention "synced".
    expect(within(card).getAllByText(/synced/i).length).toBeGreaterThanOrEqual(1);
    expect(within(card).getByText(/owner only/i)).toBeInTheDocument();
    // Indexed count is locale-formatted.
    expect(within(card).getByText('1,284')).toBeInTheDocument();
  });

  it('truncates the URL line rather than breaking layout', () => {
    const longUrl = 'https://example.com/' + 'very-long-path-segment-'.repeat(12) + 'end';
    render(
      <SourceCard
        source={makeSource({ config: { url: longUrl, mode: 'page' } })}
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByTitle(longUrl).className).toMatch(/truncate/);
  });

  it('shows the last error for a failed source', () => {
    render(
      <SourceCard
        source={makeSource({ status: 'error', last_error: 'Fetch failed: 503', indexed_count: 0 })}
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText(/fetch failed: 503/i)).toBeInTheDocument();
  });

  it('fires onSync / onRemove from keyboard-reachable buttons', async () => {
    const onSync = vi.fn();
    const onRemove = vi.fn();
    const user = userEvent.setup();
    const source = makeSource();
    render(<SourceCard source={source} onSync={onSync} onRemove={onRemove} />);

    await user.click(screen.getByRole('button', { name: /sync now/i }));
    expect(onSync).toHaveBeenCalledWith(source);

    await user.click(screen.getByRole('button', { name: /remove handbook/i }));
    expect(onRemove).toHaveBeenCalledWith(source);
  });

  it('disables the sync button while a sync is in flight', () => {
    render(<SourceCard source={makeSource()} syncing onSync={() => {}} onRemove={() => {}} />);
    expect(screen.getByRole('button', { name: /syncing/i })).toBeDisabled();
  });

  it('keeps web actions for a NON-admin (web sources stay owner-scoped)', () => {
    render(
      <SourceCard source={makeSource()} isAdmin={false} onSync={() => {}} onRemove={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /sync now/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /remove/i })).toBeInTheDocument();
  });
});

describe('SourceCard — gdrive health surface (ADR-0019 §5)', () => {
  it('shows the connected account email, ACL freshness, and the mirrored-permission pill', () => {
    render(<SourceCard source={makeGdrive()} isAdmin onSync={() => {}} onRemove={() => {}} />);
    const card = screen.getByRole('article', { name: /google drive/i });
    expect(within(card).getByText('drive-ops@acme.com')).toBeInTheDocument();
    expect(within(card).getByText(/permissions mirrored/i)).toBeInTheDocument();
    expect(within(card).getByText(/source permissions/i)).toBeInTheDocument();
    // No "Owner only" pill on a managed source — access mirrors the source ACL.
    expect(within(card).queryByText(/owner only/i)).not.toBeInTheDocument();
  });

  it('shows "Not connected yet" before OAuth completes (connected_account null)', () => {
    render(
      <SourceCard
        source={makeGdrive({
          status: 'pending_auth',
          connected_account: null,
          acl_synced_at: null,
          unmapped_acl_count: null,
        })}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText(/not connected yet/i)).toBeInTheDocument();
    expect(screen.getByText(/permissions not yet mirrored/i)).toBeInTheDocument();
  });

  it('surfaces the unmapped-ACL count with the attestation hint', () => {
    render(
      <SourceCard
        source={makeGdrive({ unmapped_acl_count: 17 })}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText('17')).toBeInTheDocument();
    expect(screen.getByText(/attesting member identities/i)).toBeInTheDocument();
  });

  it('ALWAYS renders the unmapped-ACL row — 0 reads as all-mapped, null as awaiting first sync', () => {
    // The field is a required contract health reading: 0 and null are
    // meaningful states, never hidden.
    const { rerender } = render(
      <SourceCard
        source={makeGdrive({ unmapped_acl_count: 0 })}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText(/unmapped access/i)).toBeInTheDocument();
    expect(screen.getByText(/every mirrored permission maps to a member/i)).toBeInTheDocument();
    rerender(
      <SourceCard
        source={makeGdrive({ unmapped_acl_count: null })}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText(/unmapped access/i)).toBeInTheDocument();
    expect(screen.getByText(/not available until the first permissions sync/i)).toBeInTheDocument();
  });
});

describe('SourceCard — connect / reauthorize visibility', () => {
  it('offers Connect (not Sync) to an admin on a pending_auth source', async () => {
    const onConnect = vi.fn();
    const user = userEvent.setup();
    const source = makeGdrive({ status: 'pending_auth', connected_account: null });
    render(
      <SourceCard
        source={source}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={onConnect}
      />,
    );
    expect(screen.queryByRole('button', { name: /sync now/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /connect/i }));
    expect(onConnect).toHaveBeenCalledWith(source);
  });

  it('offers Reauthorize to an admin when reauthorize_required, with the dead-grant warning', async () => {
    const onConnect = vi.fn();
    const user = userEvent.setup();
    const source = makeGdrive({ status: 'error', reauthorize_required: true });
    render(
      <SourceCard
        source={source}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={onConnect}
      />,
    );
    expect(screen.getByText(/expired or was revoked/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /reauthorize/i }));
    expect(onConnect).toHaveBeenCalledWith(source);
  });

  it('shows NO Reauthorize action on a healthy gdrive card (reauthorize_required false)', () => {
    render(
      <SourceCard
        source={makeGdrive()}
        isAdmin
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: /reauthorize/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sync now/i })).toBeInTheDocument();
  });

  it('disables Connect while the flow is starting', () => {
    render(
      <SourceCard
        source={makeGdrive({ status: 'pending_auth', connected_account: null })}
        isAdmin
        connecting
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /starting/i })).toBeDisabled();
  });

  it('renders a connect error inline (e.g. a direct 403 — never a blank pane)', () => {
    render(
      <SourceCard
        source={makeGdrive({ status: 'pending_auth', connected_account: null })}
        isAdmin
        connectError={new ApiError('Only a tenant admin can connect a managed source.', 403)}
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={() => {}}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(/tenant admin/i);
  });
});

describe('SourceCard — non-admin managed negative (INV-5)', () => {
  it('renders NO affordances on a gdrive card for a non-admin — health only', () => {
    render(
      <SourceCard
        source={makeGdrive({ status: 'error', reauthorize_required: true, unmapped_acl_count: 3 })}
        isAdmin={false}
        onSync={() => {}}
        onRemove={() => {}}
        onConnect={() => {}}
      />,
    );
    // No managed-source mutations of any kind (create/connect/sync/delete are
    // admin-gated at action time, ADR-0019 §1).
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    // The health surface still reads.
    expect(screen.getByText('drive-ops@acme.com')).toBeInTheDocument();
    expect(screen.getByText(/attesting member identities/i)).toBeInTheDocument();
  });
});
