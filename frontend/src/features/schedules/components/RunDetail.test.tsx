/**
 * State-coverage tests for the RunDetail view (#237, AC-2/AC-3). Covers loading,
 * error+retry, a succeeded run rendering the transcript + answer + citations +
 * outputs, and the negative case: a failed/escalated run surfaces its typed error
 * prominently — never a blank pane (AC-3).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { Run } from '@/api';

const getRun = vi.fn<() => Promise<Run>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getRun: () => getRun() };
});

vi.mock('@/components/DocumentPreviewBody', () => ({
  DocumentPreviewBody: ({ initialTimeMs }: { initialTimeMs?: number }) => (
    <div data-testid="run-media-preview">{initialTimeMs}</div>
  ),
}));

import { RunDetail } from './RunDetail';

const SUCCEEDED: Run = {
  id: 'r1',
  schedule_id: 's1',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'succeeded',
  summary: 'Digest generated',
  inputs: { topic: 'weekly' },
  outputs: { headline: 'All systems nominal' },
  steps: [
    { seq: 0, kind: 'delta', payload: { text: 'The answer is ' }, created_at: 't' },
    { seq: 2, kind: 'delta', payload: { text: 'grounded.' }, created_at: 't' },
    { seq: 1, kind: 'tool_call', payload: { name: 'search' }, created_at: 't' },
  ],
  citations: [
    {
      id: 'c1',
      document_id: 'd1',
      document_name: 'Q3 Revenue.pdf',
      chunk_id: 'k1',
      snippet: 'Revenue grew 12% QoQ.',
      char_start: 0,
      char_end: 21,
    },
  ],
  started_at: '2026-07-02T08:00:00Z',
  finished_at: '2026-07-02T08:01:00Z',
  created_at: '2026-07-02T08:00:00Z',
};

const FAILED: Run = {
  id: 'r2',
  assistant_id: 'a1',
  trigger: 'manual',
  status: 'failed',
  error: { code: 'model_unavailable', message: 'The model was temporarily unavailable.' },
  created_at: '2026-07-02T09:00:00Z',
};

const ESCALATED: Run = {
  id: 'r3',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'escalated',
  error: { code: 'ambiguous', message: 'The request was ambiguous.' },
  created_at: '2026-07-02T10:00:00Z',
};

function render(id = 'r1') {
  return renderWithQuery(
    <MemoryRouter>
      <RunDetail runId={id} />
    </MemoryRouter>,
  );
}

beforeEach(() => getRun.mockReset());

describe('RunDetail', () => {
  it('renders a loading skeleton', () => {
    getRun.mockReturnValue(new Promise<Run>(() => {}));
    render();
    expect(screen.getByText(/loading run/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on error', async () => {
    getRun.mockRejectedValue(new ApiError('boom', 500));
    render();
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 404 as existence non-disclosure without a retry (INV-1/INV-2)', async () => {
    getRun.mockRejectedValue(new ApiError('nope', 404));
    render();
    expect(await screen.findByText(/no longer exists/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the answer, transcript summary, and outputs on a succeeded run (AC-2)', async () => {
    getRun.mockResolvedValue(SUCCEEDED);
    render();
    expect(await screen.findByText(/the answer is grounded/i)).toBeInTheDocument();
    expect(screen.getByText(/1 tool call · 1 citation/i)).toBeInTheDocument();
    expect(screen.getByText(/All systems nominal/)).toBeInTheDocument();
    expect(screen.getByText('Digest generated')).toBeInTheDocument();
  });

  it('opens a citation into the source inspector (AC-2)', async () => {
    getRun.mockResolvedValue(SUCCEEDED);
    const user = userEvent.setup();
    render();
    const chip = await screen.findByRole('button', { name: /citation 1: q3 revenue.pdf/i });
    await user.click(chip);
    // The inspector renders in a desktop side pane AND a mobile panel (responsive);
    // jsdom keeps both in the tree, so at least one shows the cited passage.
    const inspectors = await screen.findAllByRole('region', { name: /source: q3 revenue.pdf/i });
    expect(inspectors.length).toBeGreaterThanOrEqual(1);
    expect(within(inspectors[0]!).getByText(/Revenue grew 12% QoQ/)).toBeInTheDocument();
  });

  it('does not mount a media player for REST citation fields serialized as null', async () => {
    getRun.mockResolvedValue({
      ...SUCCEEDED,
      citations: [
        {
          ...SUCCEEDED.citations![0]!,
        },
      ],
    });
    const user = userEvent.setup();
    render();

    await user.click(await screen.findByRole('button', { name: /citation 1: q3 revenue.pdf/i }));
    expect(screen.queryByTestId('run-media-preview')).not.toBeInTheDocument();
  });

  it('surfaces a FAILED run’s typed error prominently — not a blank pane (AC-3)', async () => {
    getRun.mockResolvedValue(FAILED);
    render('r2');
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/this run failed/i)).toBeInTheDocument();
    expect(within(alert).getByText(/temporarily unavailable/i)).toBeInTheDocument();
    expect(within(alert).getByText(/model_unavailable/i)).toBeInTheDocument();
  });

  it('surfaces an ESCALATED run as needing a human (AC-3)', async () => {
    getRun.mockResolvedValue(ESCALATED);
    render('r3');
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/it needs a human/i)).toBeInTheDocument();
    expect(within(alert).getByText(/the request was ambiguous/i)).toBeInTheDocument();
    // The typed error code is surfaced too (never a raw vendor string).
    expect(within(alert).getByText(/code: ambiguous/i)).toBeInTheDocument();
  });
});
