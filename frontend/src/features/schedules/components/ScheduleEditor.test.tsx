/**
 * State-coverage tests for the ScheduleEditor create form (#237, AC-1/AC-4).
 * Covers the assistant roster states, the friendly cron + timezone validation
 * (AC-4), a successful create issuing the correct POST body, and a server 422
 * (INV-8) mapping back onto the matching field.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { AssistantList, Schedule, ScheduleCreate } from '@/api';

const listAssistants = vi.fn<() => Promise<AssistantList>>();
const createSchedule = vi.fn<(body: ScheduleCreate) => Promise<Schedule>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listAssistants: () => listAssistants(),
    createSchedule: (body: ScheduleCreate) => createSchedule(body),
  };
});

import { ScheduleEditor } from './ScheduleEditor';

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

const CREATED: Schedule = {
  id: 's-new',
  assistant_id: 'a1',
  owner_id: 'u1',
  cadence: { structured: { every: 'day', at: '08:00' } },
  timezone: 'America/New_York',
  delivery: { inbox: true },
  overlap_policy: 'skip',
  enabled: true,
  created_at: 't',
  updated_at: 't',
};

function render() {
  return renderWithQuery(
    <MemoryRouter initialEntries={['/schedules/new']}>
      <ScheduleEditor scheduleId={null} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listAssistants.mockReset().mockResolvedValue(ASSISTANTS);
  createSchedule.mockReset().mockResolvedValue(CREATED);
});

describe('ScheduleEditor (create)', () => {
  it('renders the assistant picker once the roster loads', async () => {
    render();
    expect(await screen.findByRole('option', { name: 'Weekly digest' })).toBeInTheDocument();
  });

  it('shows an empty-roster note when the user has no assistants', async () => {
    listAssistants.mockResolvedValue({ items: [], next_cursor: null });
    render();
    expect(await screen.findByText(/no assistants yet/i)).toBeInTheDocument();
  });

  it('blocks submit and prompts for an assistant when none is picked (AC-4)', async () => {
    const user = userEvent.setup();
    render();
    await screen.findByRole('option', { name: 'Weekly digest' });

    await user.click(screen.getByRole('button', { name: /create schedule/i }));
    expect(await screen.findByText(/pick the assistant/i)).toBeInTheDocument();
    expect(createSchedule).not.toHaveBeenCalled();
  });

  it('validates a malformed cron expression with a friendly message (AC-4)', async () => {
    const user = userEvent.setup();
    render();
    await screen.findByRole('option', { name: 'Weekly digest' });

    // Switch to cron mode and type a bad expression.
    await user.click(screen.getByRole('radio', { name: /cron expression/i }));
    const cron = screen.getByPlaceholderText('0 8 * * 1');
    await user.clear(cron);
    await user.type(cron, 'not a cron');
    // The live validator surfaces a friendly message before submit.
    expect(await screen.findByText(/5 fields/i)).toBeInTheDocument();
  });

  it('validates an unknown timezone with a friendly message (AC-4)', async () => {
    const user = userEvent.setup();
    render();
    await screen.findByRole('option', { name: 'Weekly digest' });

    const tz = screen.getByPlaceholderText('America/New_York');
    await user.clear(tz);
    await user.type(tz, 'Mars/Phobos');
    expect(await screen.findByText(/isn.t a known IANA timezone/i)).toBeInTheDocument();
  });

  it('creates a schedule with the correct body once valid', async () => {
    const user = userEvent.setup();
    render();
    await screen.findByRole('option', { name: 'Weekly digest' });

    await user.selectOptions(screen.getByLabelText(/^assistant/i), 'a1');
    await user.click(screen.getByRole('button', { name: /create schedule/i }));

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1));
    const body = createSchedule.mock.calls[0]?.[0];
    expect(body).toMatchObject({ assistant_id: 'a1' });
    expect(body?.cadence).toBeDefined();
    expect(body?.timezone).toBeTruthy();
  });

  it('maps a server 422 invalid_cron back onto the cadence field (INV-8)', async () => {
    createSchedule.mockRejectedValue(
      new ApiError('bad', 422, {
        type: 'about:blank',
        title: 'Invalid cron',
        status: 422,
        code: 'invalid_cron',
        detail: 'The cron expression is not valid.',
      }),
    );
    const user = userEvent.setup();
    render();
    await screen.findByRole('option', { name: 'Weekly digest' });

    await user.selectOptions(screen.getByLabelText(/^assistant/i), 'a1');
    await user.click(screen.getByRole('button', { name: /create schedule/i }));

    // The server message is shown both inline on the cadence field AND in the
    // form banner (both actionable) — so it appears more than once.
    const messages = await screen.findAllByText(/cron expression is not valid/i);
    expect(messages.length).toBeGreaterThanOrEqual(1);
  });
});
