/**
 * State-coverage tests for the RunInbox (#238, AC-1/AC-3). Covers loading, empty
 * (unread vs. genuinely empty), error+retry, the success list, that a failed
 * delivery is flagged inline (AC-3 — never a quiet row), each delivery links to its
 * run, and mark-read calls the boundary + invalidates the inbox.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { RunDelivery, RunDeliveryList } from '@/api';

const listRunDeliveries = vi.fn<() => Promise<RunDeliveryList>>();
const markRunDeliveryRead = vi.fn<(id: string) => Promise<RunDelivery>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listRunDeliveries: () => listRunDeliveries(),
    markRunDeliveryRead: (id: string) => markRunDeliveryRead(id),
  };
});

import { RunInbox } from './RunInbox';

const DELIVERED: RunDelivery = {
  id: 'd1',
  run_id: 'r1',
  schedule_id: 's1',
  kind: 'inbox',
  status: 'delivered',
  summary: 'Weekly digest generated',
  created_at: '2026-07-03T08:00:00Z',
  read_at: null,
};

const FAILED: RunDelivery = {
  id: 'd2',
  run_id: 'r2',
  schedule_id: null,
  kind: 'inbox',
  status: 'failed',
  summary: null,
  created_at: '2026-07-03T09:00:00Z',
  read_at: null,
};

function page(items: RunDelivery[], next_cursor: string | null = null): RunDeliveryList {
  return { items, next_cursor };
}

function render() {
  return renderWithQuery(
    <MemoryRouter>
      <RunInbox />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listRunDeliveries.mockReset();
  markRunDeliveryRead.mockReset().mockResolvedValue({ ...DELIVERED, status: 'read' });
});

describe('RunInbox', () => {
  it('renders skeleton rows while loading', () => {
    listRunDeliveries.mockReturnValue(new Promise<RunDeliveryList>(() => {}));
    render();
    expect(screen.getByText(/loading your inbox/i)).toBeInTheDocument();
  });

  it('renders the genuinely-empty state', async () => {
    listRunDeliveries.mockResolvedValue(page([]));
    render();
    expect(await screen.findByText(/no run deliveries yet/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on error', async () => {
    listRunDeliveries.mockRejectedValue(new ApiError('boom', 500));
    render();
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('lists a delivery with its summary and a link to the run (AC-1)', async () => {
    listRunDeliveries.mockResolvedValue(page([DELIVERED]));
    render();
    const list = await screen.findByRole('list', { name: /run deliveries/i });
    expect(within(list).getByText('Weekly digest generated')).toBeInTheDocument();
    const link = within(list).getByRole('link', { name: /open run/i });
    expect(link).toHaveAttribute('href', '/runs/r1');
  });

  it('flags a FAILED delivery inline — never a silent drop (AC-3)', async () => {
    listRunDeliveries.mockResolvedValue(page([FAILED]));
    render();
    await screen.findByRole('list', { name: /run deliveries/i });
    expect(screen.getByText(/could not be produced/i)).toBeInTheDocument();
  });

  it('marks a delivery read via the boundary', async () => {
    listRunDeliveries.mockResolvedValue(page([DELIVERED]));
    render();
    await screen.findByRole('list', { name: /run deliveries/i });
    fireEvent.click(screen.getByRole('button', { name: /mark read/i }));
    await waitFor(() => expect(markRunDeliveryRead).toHaveBeenCalledWith('d1'));
  });

  it('shows the caught-up empty state under the unread filter', async () => {
    listRunDeliveries.mockResolvedValue(page([]));
    render();
    await screen.findByText(/no run deliveries yet/i);
    fireEvent.click(screen.getByRole('checkbox', { name: /unread only/i }));
    expect(await screen.findByText(/all caught up/i)).toBeInTheDocument();
  });
});
