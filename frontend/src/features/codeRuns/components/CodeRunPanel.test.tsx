/**
 * CodeRunPanel state coverage (#232) — the collapsible inline chat panel. Covers:
 * a live in-flight run renders its streamed output immediately (before the record
 * loads); expanding fetches + shows the full record; the read endpoint's loading
 * and error+retry states; and a live-activity fallback when the record read fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { CodeRun } from '@/api';
import type { CodeRunActivity } from '@/features/chat/model/streamReducer';

const getCodeRun = vi.fn<() => Promise<CodeRun>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getCodeRun: () => getCodeRun() };
});

import { CodeRunPanel } from './CodeRunPanel';

const RECORD: CodeRun = {
  id: 'run-1',
  status: 'succeeded',
  code: 'print("hi")',
  stdout: 'hi\n',
  stderr: '',
  exit_code: 0,
  duration_ms: 120,
  resource_usage: null,
  artifact_ids: [],
  requested_packages: [],
  created_at: '2026-07-02T00:00:00Z',
};

const LIVE: CodeRunActivity = {
  runId: 'run-1',
  callId: 'call-1',
  status: 'running',
  stdout: 'streaming…',
  stderr: '',
  artifactIds: [],
};

beforeEach(() => getCodeRun.mockReset());

describe('CodeRunPanel', () => {
  it('renders a live in-flight run’s streamed output immediately, before the record loads (AC-1)', async () => {
    // The read never resolves — the live activity must still render (no blank wait).
    getCodeRun.mockReturnValue(new Promise<CodeRun>(() => {}));
    renderWithQuery(<CodeRunPanel runId="run-1" activity={LIVE} defaultOpen />);
    const stdout = await screen.findByLabelText('stdout output');
    expect(within(stdout).getByText(/streaming/)).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
  });

  it('expands to fetch and show the full record (code + exit + duration) (AC-1)', async () => {
    getCodeRun.mockResolvedValue(RECORD);
    const user = userEvent.setup();
    renderWithQuery(<CodeRunPanel runId="run-1" />);
    // Collapsed by default (no live activity) — expand it.
    await user.click(screen.getByRole('button', { name: /code run/i }));
    expect(await screen.findByText(/print/)).toBeInTheDocument();
    expect(screen.getByText('Succeeded')).toBeInTheDocument();
    expect(screen.getByText('120ms')).toBeInTheDocument();
  });

  it('shows a loading skeleton while the record read is pending (no live activity)', async () => {
    getCodeRun.mockReturnValue(new Promise<CodeRun>(() => {}));
    renderWithQuery(<CodeRunPanel runId="run-1" defaultOpen />);
    expect(await screen.findByText(/loading code run/i)).toBeInTheDocument();
  });

  it('surfaces an actionable retry when the record read fails (no live fallback)', async () => {
    getCodeRun.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<CodeRunPanel runId="run-1" defaultOpen />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 404 as existence non-disclosure without a retry (INV-1/INV-2)', async () => {
    getCodeRun.mockRejectedValue(new ApiError('nope', 404));
    renderWithQuery(<CodeRunPanel runId="run-1" defaultOpen />);
    expect(await screen.findByText(/no longer exists/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('keeps showing live output when the record read fails, degraded not dead', async () => {
    getCodeRun.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<CodeRunPanel runId="run-1" activity={LIVE} defaultOpen />);
    // The degraded note appears once the read settles to an error; the live output
    // stays rendered throughout (never a blank pane).
    expect(await screen.findByText(/showing the live output only/i)).toBeInTheDocument();
    const stdout = screen.getByLabelText('stdout output');
    expect(within(stdout).getByText(/streaming/)).toBeInTheDocument();
  });

  it('does not fetch until expanded (lazy — avoids N reads on chat load)', async () => {
    getCodeRun.mockResolvedValue(RECORD);
    const user = userEvent.setup();
    // A terminal live activity means the panel is collapsed by default.
    renderWithQuery(
      <CodeRunPanel runId="run-1" activity={{ ...LIVE, status: 'succeeded', exitCode: 0 }} />,
    );
    expect(getCodeRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /code run/i }));
    // Now it fetches.
    expect(await screen.findByText(/print/)).toBeInTheDocument();
    expect(getCodeRun).toHaveBeenCalled();
  });
});
