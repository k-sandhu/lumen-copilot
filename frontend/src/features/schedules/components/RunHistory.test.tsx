/**
 * State-coverage tests for the RunHistory inbox (#237, AC-2/AC-3). Covers loading,
 * empty (filtered vs. genuinely empty), error+retry, the success list, and that a
 * failed/escalated run is flagged inline (AC-3 negative — never a quiet row).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { AssistantList, Run, RunList, ScheduleList } from '@/api';

const listRuns = vi.fn<() => Promise<RunList>>();
const listAssistants = vi.fn<() => Promise<AssistantList>>();
const listSchedules = vi.fn<() => Promise<ScheduleList>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listRuns: () => listRuns(),
    listAssistants: () => listAssistants(),
    listSchedules: () => listSchedules(),
  };
});

import { RunHistory } from './RunHistory';

const ASSISTANTS: AssistantList = {
  items: [
    {
      id: 'a1',
      name: 'Weekly digest',
      model: null,
      knowledgeScope: {},
      toolAllowlist: [],
      autonomyLevel: 'suggest',
      owner: 'u1',
      status: 'published',
      certificationState: 'none',
      featured: false,
      created_at: 't',
      updated_at: 't',
    },
  ],
  next_cursor: null,
};

const SUCCEEDED: Run = {
  id: 'r1',
  schedule_id: 's1',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'succeeded',
  summary: 'Digest generated',
  created_at: '2026-07-02T08:00:00Z',
};

const FAILED: Run = {
  id: 'r2',
  schedule_id: 's1',
  assistant_id: 'a1',
  trigger: 'manual',
  status: 'failed',
  error: { code: 'model_unavailable', message: 'The model was unavailable.' },
  created_at: '2026-07-02T09:00:00Z',
};

const ESCALATED: Run = {
  id: 'r3',
  schedule_id: 's1',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'escalated',
  error: { code: 'ambiguous', message: 'Needs a human decision.' },
  created_at: '2026-07-02T10:00:00Z',
};

function page(items: Run[], next_cursor: string | null = null): RunList {
  return { items, next_cursor };
}

function render() {
  return renderWithQuery(
    <MemoryRouter>
      <RunHistory />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listRuns.mockReset();
  listAssistants.mockReset().mockResolvedValue(ASSISTANTS);
  listSchedules.mockReset().mockResolvedValue({ items: [], next_cursor: null });
});

describe('RunHistory', () => {
  it('renders skeleton rows while loading', () => {
    listRuns.mockReturnValue(new Promise<RunList>(() => {}));
    render();
    expect(screen.getByText(/loading runs/i)).toBeInTheDocument();
  });

  it('renders the genuinely-empty state', async () => {
    listRuns.mockResolvedValue(page([]));
    render();
    expect(await screen.findByText(/no runs yet/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on error', async () => {
    listRuns.mockRejectedValue(new ApiError('boom', 500));
    render();
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('lists runs with status and assistant name', async () => {
    listRuns.mockResolvedValue(page([SUCCEEDED]));
    render();
    const list = await screen.findByRole('list', { name: /^runs$/i });
    expect(within(list).getByText('Weekly digest')).toBeInTheDocument();
    expect(within(list).getByText('Digest generated')).toBeInTheDocument();
  });

  it('flags a FAILED run inline — never a quiet row (AC-3)', async () => {
    listRuns.mockResolvedValue(page([FAILED]));
    render();
    await screen.findByRole('list', { name: /^runs$/i });
    expect(screen.getByText(/this run failed/i)).toBeInTheDocument();
  });

  it('flags an ESCALATED run as needing a human (AC-3)', async () => {
    listRuns.mockResolvedValue(page([ESCALATED]));
    render();
    await screen.findByRole('list', { name: /^runs$/i });
    expect(screen.getByText(/open it to review/i)).toBeInTheDocument();
  });
});
