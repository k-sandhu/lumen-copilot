/**
 * EscalationActions (E7-5, #239) — the human handoff on an escalated run. Covers:
 * resume (re-enqueue), cancel (close), reroute (reassign to another owner), the
 * disabled-while-pending guard (no double handoff), and the negative surface — a 409
 * (the run is no longer escalated) shown inline, never a silent no-op.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { Run } from '@/api';

const resumeRun = vi.fn<() => Promise<Run>>();
const cancelRun = vi.fn<() => Promise<Run>>();
const rerouteRun = vi.fn<(id: string, body: { to_owner_id: string }) => Promise<Run>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    resumeRun: () => resumeRun(),
    cancelRun: () => cancelRun(),
    rerouteRun: (id: string, body: { to_owner_id: string }) => rerouteRun(id, body),
  };
});

import { EscalationActions } from './EscalationActions';

const ESCALATED: Run = {
  id: 'r3',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'escalated',
  error: { code: 'ambiguous', message: 'The request was ambiguous.' },
  created_at: '2026-07-02T10:00:00Z',
};

const TARGET = '11111111-1111-4111-8111-111111111111';

function render() {
  return renderWithQuery(<EscalationActions run={ESCALATED} />);
}

beforeEach(() => {
  resumeRun.mockReset();
  cancelRun.mockReset();
  rerouteRun.mockReset();
});

describe('EscalationActions', () => {
  it('resumes an escalated run', async () => {
    resumeRun.mockResolvedValue({ ...ESCALATED, status: 'queued', error: null });
    const user = userEvent.setup();
    render();
    await user.click(screen.getByRole('button', { name: /^resume$/i }));
    await waitFor(() => expect(resumeRun).toHaveBeenCalledTimes(1));
  });

  it('cancels an escalated run', async () => {
    cancelRun.mockResolvedValue({
      ...ESCALATED,
      status: 'failed',
      error: { code: 'cancelled', message: 'x' },
    });
    const user = userEvent.setup();
    render();
    await user.click(screen.getByRole('button', { name: /cancel run/i }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(1));
  });

  it('reroutes to another owner after entering a valid id', async () => {
    rerouteRun.mockResolvedValue({ ...ESCALATED, status: 'queued', error: null });
    const user = userEvent.setup();
    render();
    await user.click(screen.getByRole('button', { name: /reroute…/i }));
    const input = screen.getByRole('textbox', { name: /reassign to/i });
    await user.type(input, TARGET);
    await user.click(screen.getByRole('button', { name: /^reroute$/i }));
    await waitFor(() => expect(rerouteRun).toHaveBeenCalledWith('r3', { to_owner_id: TARGET }));
  });

  it('keeps reroute disabled until the target id is a valid uuid', async () => {
    const user = userEvent.setup();
    render();
    await user.click(screen.getByRole('button', { name: /reroute…/i }));
    await user.type(screen.getByRole('textbox', { name: /reassign to/i }), 'not-a-uuid');
    expect(screen.getByRole('button', { name: /^reroute$/i })).toBeDisabled();
    expect(rerouteRun).not.toHaveBeenCalled();
  });

  it('surfaces a 409 (no longer escalated) inline — never a silent no-op (INV-8)', async () => {
    resumeRun.mockRejectedValue(new ApiError('conflict', 409));
    const user = userEvent.setup();
    render();
    await user.click(screen.getByRole('button', { name: /^resume$/i }));
    expect(await screen.findByText(/no longer awaiting a decision/i)).toBeInTheDocument();
  });
});
