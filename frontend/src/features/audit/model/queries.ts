/**
 * Server-state hooks for the audit slice (#86) — TanStack Query over the typed
 * `api/audit` boundary (the ONLY backend caller, ADR-0004). The audit trail is
 * server data; filters are part of the query key so each filter combination
 * caches independently and re-fetches when the filter changes.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §audit, #80) via the
 * api/ boundary only — no transport here. Default ordering newest → oldest is
 * the server's; the screen pages forward with the returned `next_cursor`.
 */
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { listAuditEvents } from '@/api';
import type { AuditEventList, AuditEventType } from '@/api';

/** UI-facing filter state for the audit screen (drives both query + URL later). */
export interface AuditFilters {
  /** One actor (user id, or `system`/`anonymous`). */
  actor?: string;
  event_type?: AuditEventType;
  resource_id?: string;
  /** Inclusive lower bound (datetime-local string, converted to ISO on the wire). */
  from?: string;
  /** Exclusive upper bound (datetime-local string, converted to ISO on the wire). */
  to?: string;
  /** Forward pagination cursor for the current page. */
  cursor?: string;
}

/** Default page size requested from the server. */
export const AUDIT_PAGE_LIMIT = 50;

/** Stable, structural query key — each distinct filter set caches on its own. */
export function auditKey(filters: AuditFilters) {
  return [
    'audit',
    filters.actor ?? null,
    filters.event_type ?? null,
    filters.resource_id ?? null,
    filters.from ?? null,
    filters.to ?? null,
    filters.cursor ?? null,
  ] as const;
}

/**
 * A page of audit events for the given filters (admin / security only — the
 * api/ boundary surfaces 401/403/422 as typed `ApiError`s the screen renders).
 * `keepPreviousData` keeps the table visible (not flashing to a spinner) while
 * the next page or a changed filter loads.
 */
export function useAuditEvents(filters: AuditFilters): UseQueryResult<AuditEventList> {
  return useQuery<AuditEventList>({
    queryKey: auditKey(filters),
    queryFn: ({ signal }) =>
      listAuditEvents(
        {
          actor: filters.actor,
          event_type: filters.event_type,
          resource_id: filters.resource_id,
          from: filters.from,
          to: filters.to,
          cursor: filters.cursor,
          limit: AUDIT_PAGE_LIMIT,
        },
        signal,
      ),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
  });
}
