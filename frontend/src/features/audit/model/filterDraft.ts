/**
 * Form-local filter draft for the audit screen (#86) and its conversion to the
 * wire-facing query shape. Kept out of the component file so the component module
 * only exports a component (react-refresh constraint) and so the conversion —
 * including the `datetime-local` → UTC ISO-8601 normalization the FROZEN contract
 * expects — is independently unit-testable.
 */
import type { AuditEventType } from '@/api';
import type { AuditFilters } from './queries';

/** Draft (form-local) filters — strings the parent converts to the wire shape. */
export interface AuditFilterDraft {
  actor: string;
  event_type: '' | AuditEventType;
  resource_id: string;
  from: string;
  to: string;
}

export const EMPTY_DRAFT: AuditFilterDraft = {
  actor: '',
  event_type: '',
  resource_id: '',
  from: '',
  to: '',
};

/** True when no filter is set (drives the empty-state copy + Clear disabled). */
export function isEmptyDraft(d: AuditFilterDraft): boolean {
  return !d.actor && !d.event_type && !d.resource_id && !d.from && !d.to;
}

/** A `datetime-local` value (no tz) → a UTC ISO-8601 string for the wire. */
function toIso(local: string): string | undefined {
  if (!local) return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Draft form values → the wire-facing query filters (cursor reset by parent). */
export function draftToFilters(d: AuditFilterDraft): Omit<AuditFilters, 'cursor'> {
  return {
    actor: d.actor.trim() || undefined,
    event_type: d.event_type || undefined,
    resource_id: d.resource_id.trim() || undefined,
    from: toIso(d.from),
    to: toIso(d.to),
  };
}
