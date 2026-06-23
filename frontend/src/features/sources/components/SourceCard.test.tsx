/**
 * SourceCard (#27) — one connector card. Asserts the trust signals render (status
 * dot label, freshness, the owner-only permission pill, indexed count), that a
 * long URL is set up to truncate (not break layout), and that the per-source
 * actions are real keyboard-reachable buttons wired to their callbacks. A syncing
 * card disables its sync button so a second sync can't be double-fired.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Source } from '@/api';
import { SourceCard } from './SourceCard';

function makeSource(overrides: Partial<Source> = {}): Source {
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

describe('SourceCard', () => {
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
    const longUrl =
      'https://example.com/' + 'very-long-path-segment-'.repeat(12) + 'end';
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
    render(
      <SourceCard source={makeSource()} syncing onSync={() => {}} onRemove={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /syncing/i })).toBeDisabled();
  });
});
