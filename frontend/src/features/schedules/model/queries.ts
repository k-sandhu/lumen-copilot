/**
 * Server-state hooks for the schedules + runs slice (#237) — TanStack Query over
 * the typed `@/api` boundary (the ONLY backend caller, ADR-0004). Schedules and
 * runs are server data: lists/detail live in the query cache and the create/
 * update/delete/pause/resume/run-now mutations invalidate them so the list +
 * inbox stay current. No transport here.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §schedules, §runs;
 * ADR-0015 / #234). Negative paths surface as typed `ApiError`s the components
 * branch on (a 422 → inline cron/timezone error, INV-8; a 404 → existence
 * non-disclosure, INV-1/INV-2; a 409 → run-now conflict; a 401 → re-auth
 * dead-end, INV-4).
 *
 * The form also reads the assistants list (GET /assistants) so the user can pick
 * which saved assistant a schedule runs — a plain read, cached generously.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  createSchedule,
  deleteSchedule,
  getRun,
  getSchedule,
  listAssistants,
  listRuns,
  listSchedules,
  pauseSchedule,
  resumeSchedule,
  runScheduleNow,
  updateSchedule,
} from '@/api';
import type {
  AssistantList,
  Run,
  RunEnqueued,
  RunList,
  Schedule,
  ScheduleCreate,
  ScheduleList,
  ScheduleUpdate,
} from '@/api';
import type { SchedulePageQuery } from '@/api';
import type { RunPageQuery } from '@/api';

/** Default page size for the run inbox + schedule list. */
export const PAGE_LIMIT = 20;

/** Stable query keys for the slice. */
export const scheduleKeys = {
  all: ['schedules'] as const,
  list: (q: SchedulePageQuery) => [...scheduleKeys.all, 'list', q] as const,
  detail: (id: string) => [...scheduleKeys.all, 'detail', id] as const,
};

export const runKeys = {
  all: ['runs'] as const,
  list: (q: RunPageQuery) => [...runKeys.all, 'list', q] as const,
  detail: (id: string) => [...runKeys.all, 'detail', id] as const,
};

// --- Schedules --------------------------------------------------------------

/** A page of the caller's schedules for the given filters. */
export function useSchedules(query: SchedulePageQuery): UseQueryResult<ScheduleList> {
  return useQuery<ScheduleList>({
    queryKey: scheduleKeys.list(query),
    queryFn: ({ signal }) => listSchedules({ ...query, limit: PAGE_LIMIT }, signal),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
  });
}

/** One schedule's detail (the editor target). Disabled until an id exists. */
export function useSchedule(id: string | null): UseQueryResult<Schedule> {
  return useQuery<Schedule>({
    queryKey: scheduleKeys.detail(id ?? '∅'),
    queryFn: ({ signal }) => getSchedule(id as string, signal),
    enabled: id !== null,
  });
}

/** Create a schedule. Invalidates the list so it appears immediately. */
export function useCreateSchedule() {
  const qc = useQueryClient();
  return useMutation<Schedule, unknown, ScheduleCreate>({
    mutationFn: (body) => createSchedule(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: scheduleKeys.all }),
  });
}

/** Patch a schedule. Refreshes both the detail and the list. */
export function useUpdateSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation<Schedule, unknown, ScheduleUpdate>({
    mutationFn: (body) => updateSchedule(id, body),
    onSuccess: (updated) => {
      qc.setQueryData(scheduleKeys.detail(id), updated);
      void qc.invalidateQueries({ queryKey: scheduleKeys.all });
    },
  });
}

/** Delete a schedule (204). Refreshes the list. */
export function useDeleteSchedule() {
  const qc = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id) => deleteSchedule(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: scheduleKeys.all }),
  });
}

/**
 * Pause / resume a schedule (idempotent). The returned schedule (enabled flag +
 * recomputed next_run_at) is written straight into the cache so the toggle +
 * "Next run" cell update without a round-trip flash.
 */
export function usePauseSchedule() {
  const qc = useQueryClient();
  return useMutation<Schedule, unknown, string>({
    mutationFn: (id) => pauseSchedule(id),
    onSuccess: (updated) => {
      qc.setQueryData(scheduleKeys.detail(updated.id), updated);
      void qc.invalidateQueries({ queryKey: scheduleKeys.all });
    },
  });
}

export function useResumeSchedule() {
  const qc = useQueryClient();
  return useMutation<Schedule, unknown, string>({
    mutationFn: (id) => resumeSchedule(id),
    onSuccess: (updated) => {
      qc.setQueryData(scheduleKeys.detail(updated.id), updated);
      void qc.invalidateQueries({ queryKey: scheduleKeys.all });
    },
  });
}

/**
 * Enqueue an out-of-band manual run (202). Invalidates the run inbox so the new
 * queued run appears; the schedule list refreshes so its last-run cell can catch up.
 */
export function useRunScheduleNow() {
  const qc = useQueryClient();
  return useMutation<RunEnqueued, unknown, string>({
    mutationFn: (id) => runScheduleNow(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: runKeys.all });
      void qc.invalidateQueries({ queryKey: scheduleKeys.all });
    },
  });
}

// --- Runs -------------------------------------------------------------------

/** A page of the caller's runs (the inbox) for the given filters. */
export function useRuns(query: RunPageQuery): UseQueryResult<RunList> {
  return useQuery<RunList>({
    queryKey: runKeys.list(query),
    queryFn: ({ signal }) => listRuns({ ...query, limit: PAGE_LIMIT }, signal),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
  });
}

/**
 * One run's full detail — transcript, citations, outputs, error. Non-terminal
 * runs (queued/running) refetch on an interval so an in-progress run's status +
 * transcript catch up without a manual reload (a light-touch alternative to the
 * optional WS live-attach; ADR-0015 §3).
 */
export function useRun(id: string | null): UseQueryResult<Run> {
  return useQuery<Run>({
    queryKey: runKeys.detail(id ?? '∅'),
    queryFn: ({ signal }) => getRun(id as string, signal),
    enabled: id !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'queued' || status === 'running' ? 4_000 : false;
    },
  });
}

// --- Reference lists --------------------------------------------------------

/** The caller's assistants (the schedule form's assistant picker). */
export function useAssistantsList(): UseQueryResult<AssistantList> {
  return useQuery<AssistantList>({
    queryKey: ['assistants', 'list', 'schedules-picker'],
    queryFn: ({ signal }) => listAssistants({ limit: 100 }, signal),
    staleTime: 30_000,
  });
}
