/**
 * ScheduleList (#237, AC-1) — the `/schedules` list body. Shows each schedule
 * (assistant, cadence, timezone, next/last run, status) with an enabled toggle
 * and pause/resume + run-now buttons wired to the contract's POST endpoints, plus
 * an assistant + enabled filter and cursor pagination.
 *
 * Every async state is handled: loading (skeleton rows), empty (filtered vs.
 * genuinely empty), error (actionable retry with honest 401/404 messaging). A
 * run-now success surfaces a brief toast pointing at the run inbox so the user
 * knows where the manual run landed (AC-1/AC-2 bridge). Runs the list inside a
 * min-h-0 flex column so long lists scroll independently (frontend/AGENTS.md).
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '@/api';
import type { Assistant, Schedule } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import {
  useAssistantsList,
  usePauseSchedule,
  useResumeSchedule,
  useRunScheduleNow,
  useSchedules,
} from '../model/queries';
import { ScheduleRow } from './ScheduleRow';
import { EmptyState, ErrorState, Pagination, SkeletonRows } from './StateViews';

type EnabledFilter = 'all' | 'enabled' | 'paused';

export function ScheduleList() {
  const [assistantId, setAssistantId] = useState('');
  const [enabledFilter, setEnabledFilter] = useState<EnabledFilter>('all');
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined]);
  const [toast, setToast] = useState<string | null>(null);

  const cursor = cursorStack[cursorStack.length - 1];
  const query = useMemo(
    () => ({
      assistant_id: assistantId || undefined,
      enabled: enabledFilter === 'all' ? undefined : enabledFilter === 'enabled',
      cursor,
    }),
    [assistantId, enabledFilter, cursor],
  );

  const schedules = useSchedules(query);
  const assistants = useAssistantsList();

  const pause = usePauseSchedule();
  const resume = useResumeSchedule();
  const runNow = useRunScheduleNow();

  const assistantName = useMemo(() => nameLookup(assistants.data?.items), [assistants.data]);

  const resetPaging = () => setCursorStack([undefined]);
  const nextPage = () => {
    const next = schedules.data?.next_cursor;
    if (next) setCursorStack((s) => [...s, next]);
  };
  const prevPage = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s));

  const onToggleEnabled = (schedule: Schedule) => {
    const mutation = schedule.enabled ? pause : resume;
    mutation.mutate(schedule.id);
  };
  const onRunNow = (schedule: Schedule) => {
    runNow.mutate(schedule.id, {
      onSuccess: () => setToast(`Started a run of ${assistantName(schedule.assistant_id)}.`),
      onError: (error) =>
        setToast(
          error instanceof ApiError && error.status === 409
            ? 'Couldn’t start — the assistant is unavailable or a run is already active.'
            : 'Couldn’t start the run. Please try again.',
        ),
    });
  };

  const hasFilters = assistantId !== '' || enabledFilter !== 'all';

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-3 border-b border-border px-4 pb-4 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-foreground-muted">
            Recurring assistant runs — configure cadence, pause/resume, or run one now.
          </p>
          <Link
            to="/schedules/new"
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Icon name="plus" className="shrink-0" />
            New schedule
          </Link>
        </div>

        <form
          aria-label="Schedule filters"
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => e.preventDefault()}
        >
          <label
            htmlFor="sched-assistant"
            className="flex flex-col gap-1 text-xs text-foreground-muted"
          >
            <span>Assistant</span>
            <select
              id="sched-assistant"
              value={assistantId}
              onChange={(e) => {
                setAssistantId(e.target.value);
                resetPaging();
              }}
              className="w-52 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              <option value="">All assistants</option>
              {assistants.data?.items.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>

          <label
            htmlFor="sched-enabled"
            className="flex flex-col gap-1 text-xs text-foreground-muted"
          >
            <span>Status</span>
            <select
              id="sched-enabled"
              value={enabledFilter}
              onChange={(e) => {
                setEnabledFilter(e.target.value as EnabledFilter);
                resetPaging();
              }}
              className="w-40 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              <option value="all">All</option>
              <option value="enabled">Enabled</option>
              <option value="paused">Paused</option>
            </select>
          </label>

          <Link
            to="/runs"
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Icon name="inbox" className="shrink-0" />
            Run history
          </Link>
        </form>

        {toast ? (
          <p
            role="status"
            className="flex items-center justify-between gap-2 rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-sm"
          >
            <span>{toast}</span>
            <button
              type="button"
              onClick={() => setToast(null)}
              aria-label="Dismiss"
              className="rounded p-0.5 hover:bg-surface-muted"
            >
              <Icon name="x" />
            </button>
          </p>
        ) : null}
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-4 py-3">
          <ScheduleListBody
            query={schedules}
            hasFilters={hasFilters}
            assistantName={assistantName}
            onToggleEnabled={onToggleEnabled}
            onRunNow={onRunNow}
            togglingId={
              pause.isPending ? pause.variables : resume.isPending ? resume.variables : null
            }
            runningId={runNow.isPending ? runNow.variables : null}
          />
        </ScrollArea>
      </div>

      <Pagination
        label="Schedule pagination"
        page={cursorStack.length}
        canPrev={cursorStack.length > 1}
        canNext={Boolean(schedules.data?.next_cursor)}
        onPrev={prevPage}
        onNext={nextPage}
      />
    </div>
  );
}

type SchedulesQuery = ReturnType<typeof useSchedules>;

function ScheduleListBody({
  query,
  hasFilters,
  assistantName,
  onToggleEnabled,
  onRunNow,
  togglingId,
  runningId,
}: {
  query: SchedulesQuery;
  hasFilters: boolean;
  assistantName: (id: string) => string;
  onToggleEnabled: (schedule: Schedule) => void;
  onRunNow: (schedule: Schedule) => void;
  togglingId: string | null | undefined;
  runningId: string | null | undefined;
}) {
  if (query.isPending) return <SkeletonRows label="Loading schedules…" />;
  if (query.isError) {
    return (
      <ErrorState
        title="Couldn’t load schedules"
        error={query.error}
        onRetry={() => void query.refetch()}
        busy={query.isFetching}
      />
    );
  }

  if (query.data.items.length === 0) {
    return hasFilters ? (
      <EmptyState
        title="No matching schedules"
        message="No schedules match these filters. Clear a filter to see them all."
      />
    ) : (
      <EmptyState
        title="No schedules yet"
        message="Create a schedule to run one of your assistants on a cadence."
      />
    );
  }

  return (
    <ul aria-label="Schedules" className="space-y-3">
      {query.data.items.map((schedule) => (
        <li key={schedule.id}>
          <ScheduleRow
            schedule={schedule}
            assistantName={assistantName(schedule.assistant_id)}
            onToggleEnabled={onToggleEnabled}
            onRunNow={onRunNow}
            toggling={togglingId === schedule.id}
            running={runningId === schedule.id}
          />
        </li>
      ))}
    </ul>
  );
}

/** Build an id → assistant-name lookup, falling back to a short id. */
function nameLookup(assistants: Assistant[] | undefined): (id: string) => string {
  const map = new Map((assistants ?? []).map((a) => [a.id, a.name]));
  return (id: string) => map.get(id) ?? `Assistant ${id.slice(0, 8)}`;
}
