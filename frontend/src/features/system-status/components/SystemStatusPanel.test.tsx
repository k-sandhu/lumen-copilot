/**
 * State-coverage tests for the System-status feature (frontend/AGENTS.md: test
 * EACH state, not just success). Covers loading, error (+ actionable retry),
 * and success (dependencies listed with ok/degraded).
 *
 * The api/ boundary is mocked so these run with NO live backend (ADR-0006
 * Phase 1: build against the contract/mocks, not the running service).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { ReadinessStatus } from '@/api';

// Mock the api boundary's readiness call.
const getReadiness = vi.fn<() => Promise<ReadinessStatus>>();
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getReadiness: () => getReadiness() };
});

// Stub the WS hook so the panel doesn't open a real socket in jsdom.
vi.mock('../hooks/useHealthStream', () => ({
  useHealthStream: () => ({ connection: 'open', lastSeq: 3, lastType: 'event' }),
}));

// Imported AFTER the mocks above so it picks up the mocked module graph.
import { SystemStatusPanel } from './SystemStatusPanel';

const READY: ReadinessStatus = {
  status: 'ready',
  dependencies: [
    { name: 'postgres', ok: true },
    { name: 'redis', ok: true },
    { name: 'minio', ok: false, detail: 'bucket missing' },
  ],
};

describe('SystemStatusPanel', () => {
  beforeEach(() => {
    getReadiness.mockReset();
  });

  it('renders the loading state first', () => {
    // Never-resolving promise keeps the query pending.
    getReadiness.mockReturnValue(new Promise<ReadinessStatus>(() => {}));
    renderWithQuery(<SystemStatusPanel />);

    expect(screen.getByText(/checking backend readiness/i)).toBeInTheDocument();
    // The overall badge also shows a "Checking" pill while the query is pending.
    expect(screen.getByText('Checking')).toBeInTheDocument();
  });

  it('renders an actionable error state and retries on click', async () => {
    getReadiness
      .mockRejectedValueOnce(
        new ApiError('boom', 503, { type: 'about:blank', title: 'Down', status: 503 }),
      )
      .mockResolvedValueOnce(READY);

    renderWithQuery(<SystemStatusPanel />);

    // ERROR surfaced with the problem title and a Retry button.
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/down/i)).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: /retry/i });

    await userEvent.click(retry);

    // After retry, the success list renders.
    expect(await screen.findByText('postgres')).toBeInTheDocument();
  });

  it('renders the success state with each dependency and its ok/degraded badge', async () => {
    getReadiness.mockResolvedValue(READY);
    renderWithQuery(<SystemStatusPanel />);

    const list = await screen.findByRole('list', { name: /backend dependencies/i });
    expect(list).toBeInTheDocument();

    expect(screen.getByText('postgres')).toBeInTheDocument();
    expect(screen.getByText('redis')).toBeInTheDocument();
    expect(screen.getByText('minio')).toBeInTheDocument();

    // Two ok + one degraded badge.
    await waitFor(() => expect(screen.getAllByText('ok')).toHaveLength(2));
    expect(screen.getByText('degraded')).toBeInTheDocument();
  });
});
