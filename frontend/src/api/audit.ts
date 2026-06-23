/**
 * Typed audit-trail call — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA performs audit HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §audit, M2 #80):
 *
 *   GET /audit?actor&event_type&resource_id&from&to&cursor&limit → AuditEventList
 *
 * Reads the one append-only audit sink (spec 0004 §2.4). Restricted to the
 * `admin` and `security` roles; any other role (incl. `member`) receives 403
 * (INV-5). Events are tenant-scoped — a caller only ever sees their own tenant's
 * events (INV-1). Each event carries `provenance` (candidate allow/exclude
 * dispositions + the raw payload) so a reviewer can see WHY a decision was made.
 * Default ordering is newest → oldest; `from`/`to` bound the time window.
 *
 * Negative paths surface as typed `ApiError`s: 401 (INV-4) for a missing/expired
 * token, 403 (INV-5) for a non-admin/non-security caller, 422 (INV-8) for a
 * malformed filter.
 */
import { request } from './client';
import type { AuditEventList, AuditQuery } from './types';

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/**
 * The append-only audit trail (admin / security only). Filter by `actor`,
 * `event_type`, `resource_id`, and a `from`/`to` time window; page with
 * `cursor` / `limit`. Newest → oldest.
 */
export function listAuditEvents(
  query: AuditQuery = {},
  signal?: AbortSignal,
): Promise<AuditEventList> {
  const qs = buildQuery({
    actor: query.actor,
    event_type: query.event_type,
    resource_id: query.resource_id,
    from: query.from,
    to: query.to,
    cursor: query.cursor,
    limit: query.limit,
  });
  return request<AuditEventList>(`/audit${qs}`, { signal });
}
