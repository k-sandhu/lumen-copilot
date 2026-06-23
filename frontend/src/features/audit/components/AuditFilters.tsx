/**
 * Filter bar for the audit log (#86) — actor / event-type / resource / time
 * window. Controlled inputs; changes are debounced/applied by the parent panel,
 * which owns the filter state and the query. A "Clear" action resets to the
 * unfiltered (newest → oldest) view.
 *
 * The event-type options mirror the FROZEN contract taxonomy (api/types
 * `AuditEventType`, spec 0004 §2.4). Each input is labelled for keyboard + AT.
 */
import type { AuditEventType } from '@/api';
import { Icon } from '@/ui';
import { eventTypeLabel } from '../model/presentation';
import { isEmptyDraft, type AuditFilterDraft } from '../model/filterDraft';

/** Every wire event_type, grouped roughly by the four row kinds, for the select. */
const EVENT_TYPES: AuditEventType[] = [
  'retrieval.query',
  'answer.generated',
  'permission.denied',
  'auth.login',
  'auth.login_failed',
  'auth.logout',
  'document.viewed',
  'document.downloaded',
  'document.uploaded',
  'document.deleted',
  'collection.created',
  'action.requested',
  'action.approved',
  'action.executed',
];

interface AuditFiltersProps {
  draft: AuditFilterDraft;
  onChange: (draft: AuditFilterDraft) => void;
  onApply: () => void;
  onClear: () => void;
  /** Whether a request is currently in flight (shows a subtle indicator). */
  fetching?: boolean;
}

export function AuditFilters({ draft, onChange, onApply, onClear, fetching }: AuditFiltersProps) {
  const set = <K extends keyof AuditFilterDraft>(key: K, value: AuditFilterDraft[K]): void =>
    onChange({ ...draft, [key]: value });

  return (
    <form
      className="shrink-0 border-b border-border px-4 py-3"
      aria-label="Audit filters"
      onSubmit={(e) => {
        e.preventDefault();
        onApply();
      }}
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Actor" htmlFor="audit-actor">
          <input
            id="audit-actor"
            type="text"
            value={draft.actor}
            onChange={(e) => set('actor', e.target.value)}
            placeholder="user id, system…"
            className="w-44 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          />
        </Field>

        <Field label="Event type" htmlFor="audit-type">
          <select
            id="audit-type"
            value={draft.event_type}
            onChange={(e) => set('event_type', e.target.value as AuditFilterDraft['event_type'])}
            className="w-52 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          >
            <option value="">All event types</option>
            {EVENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {eventTypeLabel(t)}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Resource" htmlFor="audit-resource">
          <input
            id="audit-resource"
            type="text"
            value={draft.resource_id}
            onChange={(e) => set('resource_id', e.target.value)}
            placeholder="doc-…, msg-…"
            className="w-44 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          />
        </Field>

        <Field label="From" htmlFor="audit-from">
          <input
            id="audit-from"
            type="datetime-local"
            value={draft.from}
            onChange={(e) => set('from', e.target.value)}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          />
        </Field>

        <Field label="To" htmlFor="audit-to">
          <input
            id="audit-to"
            type="datetime-local"
            value={draft.to}
            onChange={(e) => set('to', e.target.value)}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          />
        </Field>

        <div className="flex items-center gap-2">
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium hover:bg-surface-muted"
          >
            <Icon name="filter" />
            Apply
          </button>
          <button
            type="button"
            onClick={onClear}
            disabled={isEmptyDraft(draft)}
            className="rounded-md px-3 py-1.5 text-sm hover:bg-surface-muted disabled:opacity-50"
          >
            Clear
          </button>
          {fetching ? (
            <span className="text-xs text-foreground-muted" aria-live="polite">
              Loading…
            </span>
          ) : null}
        </div>
      </div>
    </form>
  );
}

function Field({
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
