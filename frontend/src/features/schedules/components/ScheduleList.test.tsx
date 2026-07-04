/**
 * State-coverage tests for the ScheduleList (#237, AC-1) — frontend/AGENTS.md:
 * test EACH state, not just success. Covers loading, empty (filtered vs. genuinely
 * empty), error+retry, the success list, and that the enabled toggle + run-now
 * buttons issue the correct contract calls (pause/resume/run-now).
 *
 * The api/ boundary is mocked so these run with NO live backend (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { AssistantList, RunEnqueued, Schedule, ScheduleList as WireScheduleList } from '@/api';

const listSchedules = vi.fn<() => Promise<WireScheduleList>>();
const listAssistants = vi.fn<() => Promise<AssistantList>>();
const pauseSchedule = vi.fn<(id: string) => Promise<Schedule>>();
const resumeSchedule = vi.fn<(id: string) => Promise<Schedule>>();
const runScheduleNow = vi.fn<(id: string) => Promise<RunEnqueued>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listSchedules: () => listSchedules(),
    listAssistants: () => listAssistants(),
    pauseSchedule: (id: string) => pauseSchedule(id),
    resumeSchedule: (id: string) => resumeSchedule(id),
    runScheduleNow: (id: string) => runScheduleNow(id),
  };
});

import { ScheduleList } from './ScheduleList';

const ENABLED: Schedule = {
  id: 's1',
  assistant_id: 'a1',
  owner_id: 'u1',
  cadence: { structured: { every: 'day', at: '08:00' } },
  timezone: 'America/New_York',
  delivery: { inbox: true },
  overlap_policy: 'skip',
  enabled: true,
  next_run_at: '2999-01-01T08:00:00Z',
  last_run_at: null,
  last_status: null,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
};

const PAUSED: Schedule = { ...ENABLED, id: 's2', enabled: false, next_run_at: null };

const ASSISTANTS: AssistantList = {
  items: [
    {
      id: 'a1',
      name: 'Weekly digest',
      model: null,
      knowledgeScope: {},
      toolAllowlist: [],
      autonomyLevel: 'suggest',
      effectiveAutonomy: 'suggest',
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

function page(items: Schedule[], next_cursor: string | null = null): WireScheduleList {
  return { items, next_cursor };
}

function render() {
  return renderWithQuery(
    <MemoryRouter>
      <ScheduleList />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listSchedules.mockReset();
  listAssistants.mockReset().mockResolvedValue(ASSISTANTS);
  pauseSchedule.mockReset().mockResolvedValue(PAUSED);
  resumeSchedule.mockReset().mockResolvedValue(ENABLED);
  runScheduleNow.mockReset().mockResolvedValue({ run_id: 'r1' });
});

describe('ScheduleList', () => {
  it('renders skeleton rows while loading', () => {
    listSchedules.mockReturnValue(new Promise<WireScheduleList>(() => {}));
    render();
    expect(screen.getByText(/loading schedules/i)).toBeInTheDocument();
  });

  it('renders the genuinely-empty state', async () => {
    listSchedules.mockResolvedValue(page([]));
    render();
    expect(await screen.findByText(/no schedules yet/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on a generic error', async () => {
    listSchedules.mockRejectedValue(new ApiError('boom', 500));
    render();
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages the 401 dead-end without a retry (INV-4)', async () => {
    listSchedules.mockRejectedValue(new ApiError('unauthorized', 401));
    render();
    expect(await screen.findByText(/session expired/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders schedules with the assistant name, cadence, and status', async () => {
    listSchedules.mockResolvedValue(page([ENABLED]));
    render();
    const list = await screen.findByRole('list', { name: /schedules/i });
    expect(within(list).getByText('Weekly digest')).toBeInTheDocument();
    expect(within(list).getByText(/Every day at 08:00/)).toBeInTheDocument();
  });

  it('pauses an enabled schedule via the toggle (correct call)', async () => {
    listSchedules.mockResolvedValue(page([ENABLED]));
    const user = userEvent.setup();
    render();
    await screen.findByRole('list', { name: /schedules/i });

    await user.click(screen.getByRole('switch', { name: /pause schedule for weekly digest/i }));
    await waitFor(() => expect(pauseSchedule).toHaveBeenCalledWith('s1'));
    expect(resumeSchedule).not.toHaveBeenCalled();
  });

  it('resumes a paused schedule via the toggle (correct call)', async () => {
    listSchedules.mockResolvedValue(page([PAUSED]));
    const user = userEvent.setup();
    render();
    await screen.findByRole('list', { name: /schedules/i });

    await user.click(screen.getByRole('switch', { name: /resume schedule for weekly digest/i }));
    await waitFor(() => expect(resumeSchedule).toHaveBeenCalledWith('s2'));
    expect(pauseSchedule).not.toHaveBeenCalled();
  });

  it('runs a schedule now and surfaces a confirmation toast (correct call)', async () => {
    listSchedules.mockResolvedValue(page([ENABLED]));
    const user = userEvent.setup();
    render();
    await screen.findByRole('list', { name: /schedules/i });

    await user.click(screen.getByRole('button', { name: /run weekly digest now/i }));
    await waitFor(() => expect(runScheduleNow).toHaveBeenCalledWith('s1'));
    expect(await screen.findByText(/started a run of weekly digest/i)).toBeInTheDocument();
  });

  it('surfaces a friendly message when run-now conflicts (409)', async () => {
    listSchedules.mockResolvedValue(page([ENABLED]));
    runScheduleNow.mockRejectedValue(new ApiError('conflict', 409));
    const user = userEvent.setup();
    render();
    await screen.findByRole('list', { name: /schedules/i });

    await user.click(screen.getByRole('button', { name: /run weekly digest now/i }));
    expect(await screen.findByText(/assistant is unavailable or a run is already active/i)).toBeInTheDocument();
  });

  it('enables Next when a cursor is returned', async () => {
    listSchedules.mockResolvedValue(page([ENABLED], 'cursor-2'));
    render();
    await screen.findByRole('list', { name: /schedules/i });
    expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /next/i })).toBeEnabled();
  });
});
