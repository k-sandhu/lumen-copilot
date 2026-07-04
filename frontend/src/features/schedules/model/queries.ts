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
  cancelRun,
  createSchedule,
  deleteSchedule,
  getRun,
  getSchedule,
  listAssistants,
  listRunDeliveries,
  listRuns,
  listSchedules,
  markRunDeliveryRead,
  pauseSchedule,
  rerouteRun,
  resumeRun,
  resumeSchedule,
  runScheduleNow,
  updateSchedule,
} from '@/api';
import type {
  RunDelivery,
  RunDeliveryList,
  AssistantList,
  Run,
  RunEnqueued,
  RunList,
  RunReroute,
  Schedule,
  ScheduleCreate,
  ScheduleList,
  ScheduleUpdate,
} from '@/api';
import type { SchedulePageQuery } from '@/api';
import type { RunPageQuery } from '@/api';
import type { RunDeliveryPageQuery } from '@/api';

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

/**
 * Escalation handoff (E7-5, #239) — resume / cancel / reroute an escalated run.
 * The returned run (its new status: `queued` for resume/reroute, a `failed`+
 * `cancelled` terminal for cancel) is written straight into the detail cache so the
 * banner + actions update without a round-trip flash; the inbox is invalidated so
 * the list catches up (a resumed/rerouted run re-appears queued; a cancelled one
 * closes). A reroute moves ownership away, so the current owner will no longer see
 * the run on refetch (INV-2).
 */
function applyRunResult(qc: ReturnType<typeof useQueryClient>, updated: Run): void {
  qc.setQueryData(runKeys.detail(updated.id), updated);
  void qc.invalidateQueries({ queryKey: runKeys.all });
}

export function useResumeRun() {
  const qc = useQueryClient();
  return useMutation<Run, unknown, string>({
    mutationFn: (id) => resumeRun(id),
    onSuccess: (updated) => applyRunResult(qc, updated),
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation<Run, unknown, string>({
    mutationFn: (id) => cancelRun(id),
    onSuccess: (updated) => applyRunResult(qc, updated),
  });
}

export function useRerouteRun(id: string) {
  const qc = useQueryClient();
  return useMutation<Run, unknown, RunReroute>({
    mutationFn: (body) => rerouteRun(id, body),
    onSuccess: (updated) => applyRunResult(qc, updated),
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

export const deliveryKeys = {
  all: ['run-deliveries'] as const,
  list: (q: RunDeliveryPageQuery) => [...deliveryKeys.all, 'list', q] as const,
};

/**
 * A page of the caller's run deliveries (the in-app inbox) for the given filters.
 * A short stale time + refetch-on-focus keeps a completed run's delivery appearing
 * without a manual reload.
 */
export function useRunDeliveries(
  query: RunDeliveryPageQuery,
): UseQueryResult<RunDeliveryList> {
  return useQuery<RunDeliveryList>({
    queryKey: deliveryKeys.list(query),
    queryFn: ({ signal }) => listRunDeliveries({ ...query, limit: PAGE_LIMIT }, signal),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
  });
}

/**
 * Mark one delivery read (idempotent). Invalidates the inbox so the unread badge +
 * the read state update. A 404 (non-owned / cross-tenant) surfaces as a typed error.
 */
export function useMarkDeliveryRead() {
  const qc = useQueryClient();
  return useMutation<RunDelivery, unknown, string>({
    mutationFn: (id) => markRunDeliveryRead(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: deliveryKeys.all }),
  });
}
