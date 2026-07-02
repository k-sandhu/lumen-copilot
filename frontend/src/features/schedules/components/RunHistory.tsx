/**
 * RunHistory (#237, AC-2/AC-3) — the `/runs` inbox: the caller's runs newest
 * first, filterable by assistant, schedule, and status, with cursor pagination.
 * Every async state is handled (loading skeletons / empty / error+retry). A
 * failed/escalated run is flagged inline by RunRow so the negative case (AC-3) is
 * never a blank/quiet row. Scrolls independently inside a min-h-0 flex column.
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Assistant, RunStatus, Schedule } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useAssistantsList, useRuns, useSchedules } from '../model/queries';
import { RUN_STATUS_LABEL } from '../model/presentation';
import { RunRow } from './RunRow';
import { EmptyState, ErrorState, Pagination, SkeletonRows } from './StateViews';

const STATUSES: RunStatus[] = ['queued', 'running', 'succeeded', 'failed', 'escalated'];

export function RunHistory() {
  const [assistantId, setAssistantId] = useState('');
  const [scheduleId, setScheduleId] = useState('');
  const [status, setStatus] = useState<RunStatus | ''>('');
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined]);

  const cursor = cursorStack[cursorStack.length - 1];
  const query = useMemo(
    () => ({
      assistant_id: assistantId || undefined,
      schedule_id: scheduleId || undefined,
      status: status || undefined,
      cursor,
    }),
    [assistantId, scheduleId, status, cursor],
  );

  const runs = useRuns(query);
  const assistants = useAssistantsList();
  const schedules = useSchedules({});

  const assistantName = useMemo(() => nameLookup(assistants.data?.items), [assistants.data]);
  const scheduleLabel = useMemo(
    () => scheduleLookup(schedules.data?.items, assistantName),
    [schedules.data, assistantName],
  );

  const resetPaging = () => setCursorStack([undefined]);
  const nextPage = () => {
    const next = runs.data?.next_cursor;
    if (next) setCursorStack((s) => [...s, next]);
  };
  const prevPage = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s));

  const hasFilters = assistantId !== '' || scheduleId !== '' || status !== '';

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-3 border-b border-border px-4 pb-4 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-foreground-muted">
            Every scheduled and manual run — status, transcript, citations, and outputs.
          </p>
          <Link
            to="/schedules"
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Icon name="calendar" className="shrink-0" />
            Schedules
          </Link>
        </div>

        <form
          aria-label="Run filters"
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => e.preventDefault()}
        >
          <Filter label="Assistant" htmlFor="runs-assistant">
            <select
              id="runs-assistant"
              value={assistantId}
              onChange={(e) => {
                setAssistantId(e.target.value);
                resetPaging();
              }}
              className="w-48 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              <option value="">All assistants</option>
              {assistants.data?.items.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Filter>

          <Filter label="Schedule" htmlFor="runs-schedule">
            <select
              id="runs-schedule"
              value={scheduleId}
              onChange={(e) => {
                setScheduleId(e.target.value);
                resetPaging();
              }}
              className="w-48 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              <option value="">All (incl. manual)</option>
              {schedules.data?.items.map((s) => (
                <option key={s.id} value={s.id}>
                  {scheduleLabel(s.id)}
                </option>
              ))}
            </select>
          </Filter>

          <Filter label="Status" htmlFor="runs-status">
            <select
              id="runs-status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as RunStatus | '');
                resetPaging();
              }}
              className="w-40 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              <option value="">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {RUN_STATUS_LABEL[s]}
                </option>
              ))}
            </select>
          </Filter>
        </form>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-4 py-3">
          <RunHistoryBody query={runs} hasFilters={hasFilters} assistantName={assistantName} />
        </ScrollArea>
      </div>

      <Pagination
        label="Run pagination"
        page={cursorStack.length}
        canPrev={cursorStack.length > 1}
        canNext={Boolean(runs.data?.next_cursor)}
        onPrev={prevPage}
        onNext={nextPage}
      />
    </div>
  );
}

type RunsQuery = ReturnType<typeof useRuns>;

function RunHistoryBody({
  query,
  hasFilters,
  assistantName,
}: {
  query: RunsQuery;
  hasFilters: boolean;
  assistantName: (id: string) => string;
}) {
  if (query.isPending) return <SkeletonRows label="Loading runs…" />;
  if (query.isError) {
    return (
      <ErrorState
        title="Couldn’t load runs"
        error={query.error}
        onRetry={() => void query.refetch()}
        busy={query.isFetching}
      />
    );
  }
  if (query.data.items.length === 0) {
    return hasFilters ? (
      <EmptyState
        title="No matching runs"
        message="No runs match these filters. Clear a filter to see the full history."
      />
    ) : (
      <EmptyState
        title="No runs yet"
        message="When a schedule fires — or you run one now — its run will appear here."
      />
    );
  }
  return (
    <ul aria-label="Runs" className="space-y-3">
      {query.data.items.map((run) => (
        <li key={run.id}>
          <RunRow run={run} assistantName={assistantName(run.assistant_id)} />
        </li>
      ))}
    </ul>
  );
}

function Filter({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="flex flex-col gap-1 text-xs text-foreground-muted">
      <span>{label}</span>
      {children}
    </label>
  );
}

function nameLookup(assistants: Assistant[] | undefined): (id: string) => string {
  const map = new Map((assistants ?? []).map((a) => [a.id, a.name]));
  return (id: string) => map.get(id) ?? `Assistant ${id.slice(0, 8)}`;
}

function scheduleLookup(
  schedules: Schedule[] | undefined,
  assistantName: (id: string) => string,
): (id: string) => string {
  const map = new Map((schedules ?? []).map((s) => [s.id, s.assistant_id]));
  return (id: string) => {
    const assistantId = map.get(id);
    return assistantId ? assistantName(assistantId) : `Schedule ${id.slice(0, 8)}`;
  };
}
