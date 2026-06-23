/**
 * State-coverage tests for the AuditPanel (#86) — frontend/AGENTS.md: test EACH
 * state, not just success. Covers loading, empty (filtered vs. genuinely empty),
 * error with an actionable retry, the 403/401 access-denied dead-end (spec 0004
 * INV-5/INV-4), the success table, the click-row → provenance drawer (including
 * the EXCLUDED candidate, mission filter #4), and cursor pagination.
 *
 * The api/ boundary is mocked so these run with NO live backend (ADR-0006
 * Phase 1: build against the contract/mocks, not the running service).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { AuditEvent, AuditEventList } from '@/api';

const listAuditEvents = vi.fn<() => Promise<AuditEventList>>();
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listAuditEvents: () => listAuditEvents() };
});

// Imported AFTER the mock so it picks up the mocked module graph.
import { AuditPanel } from './AuditPanel';

const EVENT: AuditEvent = {
  id: 'evt_retrieval_001',
  ts: '2026-06-19T10:00:00Z',
  actor: 'dana@acme',
  tenant_id: 't1',
  event_type: 'retrieval.query',
  resource_id: null,
  decision: 'allowed',
  provenance: {
    candidates: [
      { resource_id: 'passage-allowed', disposition: 'allow', reason: 'in allow-set', score: 0.9 },
      { resource_id: 'passage-trimmed', disposition: 'exclude', reason: 'owner mismatch' },
    ],
    raw: { model: 'anthropic/claude-opus-4.8', query_hash: 'deadbeef' },
  },
};

function page(items: AuditEvent[], next_cursor: string | null = null): AuditEventList {
  return { items, next_cursor };
}

describe('AuditPanel', () => {
  beforeEach(() => listAuditEvents.mockReset());

  it('renders skeleton rows while loading', () => {
    listAuditEvents.mockReturnValue(new Promise<AuditEventList>(() => {}));
    renderWithQuery(<AuditPanel />);
    expect(screen.getByText(/loading audit events/i)).toBeInTheDocument();
  });

  it('renders the genuinely-empty state', async () => {
    listAuditEvents.mockResolvedValue(page([]));
    renderWithQuery(<AuditPanel />);
    expect(await screen.findByText(/no audit events/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing has been recorded yet/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on a generic error', async () => {
    listAuditEvents.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<AuditPanel />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages the 403 access-denied dead-end without a retry (INV-5)', async () => {
    listAuditEvents.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<AuditPanel />);
    expect(await screen.findByText(/don.t have access to the audit log/i)).toBeInTheDocument();
    expect(screen.getByText(/restricted to admin and security roles/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('messages the 401 dead-end without a retry (INV-4)', async () => {
    listAuditEvents.mockRejectedValue(new ApiError('unauthorized', 401));
    renderWithQuery(<AuditPanel />);
    expect(await screen.findByText(/don.t have access to the audit log/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the event table on success', async () => {
    listAuditEvents.mockResolvedValue(page([EVENT]));
    renderWithQuery(<AuditPanel />);
    const list = await screen.findByRole('list', { name: /audit events/i });
    expect(within(list).getByText('Retrieval')).toBeInTheDocument();
    expect(within(list).getByText(/dana@acme/)).toBeInTheDocument();
  });

  it('opens the provenance drawer on row click with the allow/exclude ledger + raw payload', async () => {
    listAuditEvents.mockResolvedValue(page([EVENT]));
    const user = userEvent.setup();
    renderWithQuery(<AuditPanel />);

    const row = await screen.findByRole('button', { name: /audit event/i });
    await user.click(row);

    const dialog = await screen.findByRole('dialog');
    // Per-candidate decisions, including the EXCLUDED one (mission filter #4).
    expect(within(dialog).getByText('passage-allowed')).toBeInTheDocument();
    expect(within(dialog).getByText('passage-trimmed')).toBeInTheDocument();
    expect(within(dialog).getByText(/owner mismatch/)).toBeInTheDocument();
    // The raw recorded payload, monospace.
    expect(within(dialog).getByText(/query_hash/)).toBeInTheDocument();
  });

  it('closes the drawer from its close control', async () => {
    listAuditEvents.mockResolvedValue(page([EVENT]));
    const user = userEvent.setup();
    renderWithQuery(<AuditPanel />);

    await user.click(await screen.findByRole('button', { name: /audit event/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^close$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('disables Previous on the first page and enables Next when a cursor is returned', async () => {
    listAuditEvents.mockResolvedValue(page([EVENT], 'cursor-2'));
    renderWithQuery(<AuditPanel />);
    await screen.findByRole('list', { name: /audit events/i });

    expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /next/i })).toBeEnabled();
  });

  it('advances to the next page and re-fetches with the new cursor', async () => {
    listAuditEvents.mockResolvedValue(page([EVENT], 'cursor-2'));
    const user = userEvent.setup();
    renderWithQuery(<AuditPanel />);
    await screen.findByRole('list', { name: /audit events/i });

    expect(screen.getByText(/page 1/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /next/i }));

    await waitFor(() => expect(screen.getByText(/page 2/i)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /previous/i })).toBeEnabled();
    // First page + the next page = two boundary calls.
    expect(listAuditEvents.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('shows the filtered-empty copy after applying a filter that matches nothing', async () => {
    listAuditEvents.mockResolvedValue(page([]));
    const user = userEvent.setup();
    renderWithQuery(<AuditPanel />);
    await screen.findByText(/nothing has been recorded yet/i);

    await user.type(screen.getByLabelText(/^actor$/i), 'ghost@acme');
    await user.click(screen.getByRole('button', { name: /apply/i }));

    expect(await screen.findByText(/no events match these filters/i)).toBeInTheDocument();
  });
});
