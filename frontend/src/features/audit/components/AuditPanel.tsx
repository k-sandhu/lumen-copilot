/**
 * AuditPanel (#86) — the audit-log screen body. An event table of
 * retrieval / answer / access-decision / action rows (design-system `AuditRow`),
 * a filter bar (actor / event-type / resource / time), cursor pagination, and a
 * click-row → `ProvenanceDrawer` showing the per-candidate allow/exclude ledger
 * plus the raw event payload (monospace ids).
 *
 * Every async state is handled (frontend/AGENTS.md "every state, not just
 * success"): loading (skeleton rows), empty (filtered vs. genuinely empty),
 * error (actionable retry; 403/401 messaged distinctly per spec 0004 INV-5/4).
 * The table scrolls independently inside a `min-h-0` flex column so long logs
 * never force a whole-page scroll.
 *
 * Wireframe polish (#121): a subtitle, three CLIENT-SIDE KPI tiles (events /
 * access-denied / answers-cited — each scoped honestly to the fetched page;
 * latency omitted, not faked), a client-side segmented type filter over the
 * page, a client-side CSV export of the visible rows, and a tamper-evident
 * "Append-only ledger" footer. None of this adds a backend call — it derives
 * from the page `useAuditEvents` already returned.
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import type { AuditEvent } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { AuditRow, ProvenanceDrawer, Icon } from '@/ui';
import { useAuditEvents, type AuditFilters as WireFilters } from '../model/queries';
import { toKitRow, toProvenanceDetail } from '../model/presentation';
import {
  EMPTY_DRAFT,
  draftToFilters,
  isEmptyDraft,
  type AuditFilterDraft,
} from '../model/filterDraft';
import {
  summarizeEvents,
  filterBySegment,
  type AuditSegment,
} from '../model/metrics';
import { AuditFilters } from './AuditFilters';
import { AuditKpis } from './AuditKpis';
import { AuditSegmented } from './AuditSegmented';
import { ExportButton } from './ExportButton';
import { LedgerFooter } from './LedgerFooter';

const EMPTY_EVENTS: AuditEvent[] = [];

const ALL_SEGMENTS: AuditSegment[] = ['all', 'retrieval', 'answer', 'action', 'access'];

/**
 * Per-segment counts over the fetched page, derived from `filterBySegment` so
 * each badge exactly matches what selecting that segment will show.
 */
function segmentCounts(events: readonly AuditEvent[]): Record<AuditSegment, number> {
  const counts = {} as Record<AuditSegment, number>;
  for (const seg of ALL_SEGMENTS) {
    counts[seg] = filterBySegment(events, seg).length;
  }
  return counts;
}

export function AuditPanel() {
  // Draft = what's in the form; applied = what's driving the query. Apply copies
  // draft → applied and resets pagination, so editing a field doesn't refetch
  // until the user commits it.
  const [draft, setDraft] = useState<AuditFilterDraft>(EMPTY_DRAFT);
  const [applied, setApplied] = useState<AuditFilterDraft>(EMPTY_DRAFT);
  // Cursor stack: each entry is the cursor for a page; lets us page back.
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined]);
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  // Client-side narrowing of the fetched page (issue #121); resets per page.
  const [segment, setSegment] = useState<AuditSegment>('all');

  const cursor = cursorStack[cursorStack.length - 1];
  const filters: WireFilters = useMemo(
    () => ({ ...draftToFilters(applied), cursor }),
    [applied, cursor],
  );

  const query = useAuditEvents(filters);
  const hasFilters = !isEmptyDraft(applied);

  const pageEvents = query.data?.items ?? EMPTY_EVENTS;
  const metrics = useMemo(() => summarizeEvents(pageEvents), [pageEvents]);
  const counts = useMemo(() => segmentCounts(pageEvents), [pageEvents]);
  const visibleEvents = useMemo(
    () => filterBySegment(pageEvents, segment),
    [pageEvents, segment],
  );

  const apply = (): void => {
    setApplied(draft);
    setCursorStack([undefined]);
    setSegment('all');
    setSelected(null);
  };
  const clear = (): void => {
    setDraft(EMPTY_DRAFT);
    setApplied(EMPTY_DRAFT);
    setCursorStack([undefined]);
    setSegment('all');
    setSelected(null);
  };
  const nextPage = (): void => {
    const next = query.data?.next_cursor;
    if (next) {
      setCursorStack((s) => [...s, next]);
      setSegment('all');
      setSelected(null);
    }
  };
  const prevPage = (): void => {
    setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s));
    setSegment('all');
    setSelected(null);
  };

  const detail = selected ? toProvenanceDetail(selected) : undefined;
  // Skeleton tiles only on the first load; keepPreviousData keeps them filled
  // across paging/filtering so the row never flashes empty.
  const kpisLoading = query.isPending;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-4 border-b border-border px-4 pb-4 pt-3">
        <p className="text-sm text-foreground-muted">
          Every retrieval, answer, and access decision — provable after the fact.
        </p>
        <AuditKpis metrics={metrics} loading={kpisLoading} />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <AuditSegmented value={segment} onChange={setSegment} counts={counts} />
          <ExportButton events={visibleEvents} />
        </div>
      </div>

      <AuditFilters
        draft={draft}
        onChange={setDraft}
        onApply={apply}
        onClear={clear}
        fetching={query.isFetching}
      />

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-3 py-3">
          <AuditTableBody
            query={query}
            events={visibleEvents}
            hasFilters={hasFilters}
            segment={segment}
            onSelect={setSelected}
            selectedId={selected?.id ?? null}
          />
        </ScrollArea>
      </div>

      <Pagination
        canPrev={cursorStack.length > 1}
        canNext={Boolean(query.data?.next_cursor)}
        onPrev={prevPage}
        onNext={nextPage}
        page={cursorStack.length}
        count={visibleEvents.length}
      />

      <LedgerFooter shown={visibleEvents.length} />

      <ProvenanceDrawer open={selected !== null} detail={detail} onClose={() => setSelected(null)} />
    </div>
  );
}

type AuditQueryResult = ReturnType<typeof useAuditEvents>;

function AuditTableBody({
  query,
  events,
  hasFilters,
  segment,
  onSelect,
  selectedId,
}: {
  query: AuditQueryResult;
  /** The page narrowed by the client-side segment filter (issue #121). */
  events: AuditEvent[];
  hasFilters: boolean;
  segment: AuditSegment;
  onSelect: (event: AuditEvent) => void;
  selectedId: string | null;
}) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-2">
        <span className="sr-only">Loading audit events…</span>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div
            key={i}
            className="h-14 animate-pulse rounded-md bg-surface-muted"
            aria-hidden="true"
          />
        ))}
      </div>
    );
  }

  if (query.isError) {
    return <AuditError error={query.error} onRetry={() => void query.refetch()} busy={query.isFetching} />;
  }

  const pageEmpty = query.data.items.length === 0;
  if (events.length === 0) {
    // Distinguish three empties: a server-filtered page with nothing, a
    // genuinely empty trail, and a non-empty page narrowed away by the
    // client-side segment chip (issue #121).
    const message = !pageEmpty
      ? 'No events of this type on this page. Pick another segment or page on.'
      : hasFilters
        ? 'No events match these filters. Try widening the time window or clearing a filter.'
        : 'Nothing has been recorded yet. Retrieval, answer, and access decisions will appear here.';
    return (
      <div className="px-2 py-16 text-center">
        <p className="text-sm font-medium">
          {segment === 'all' ? 'No audit events' : 'No events in this segment'}
        </p>
        <p className="mt-1 text-sm text-foreground-muted">{message}</p>
      </div>
    );
  }

  return (
    <ul aria-label="Audit events" className="space-y-1">
      {events.map((event) => (
        <li key={event.id}>
          <AuditRow
            event={toKitRow(event)}
            onSelect={() => onSelect(event)}
            className={
              selectedId === event.id ? 'ring-2 ring-accent ring-offset-1 ring-offset-surface' : undefined
            }
          />
        </li>
      ))}
    </ul>
  );
}

function AuditError({
  error,
  onRetry,
  busy,
}: {
  error: unknown;
  onRetry: () => void;
  busy: boolean;
}) {
  // Spec 0004: a non-admin/non-security caller gets 403 (INV-5); an
  // expired/missing token gets 401 (INV-4). Both are dead-ends a retry won't
  // fix, so we message them honestly instead of offering a pointless retry.
  const status = error instanceof ApiError ? error.status : 0;
  const forbidden = status === 403 || status === 401;
  const message =
    error instanceof ApiError ? error.displayMessage : 'Could not load the audit log.';

  return (
    <div role="alert" className="space-y-2 px-2 py-6 text-sm">
      <p className="flex items-center gap-2 font-medium text-danger">
        <Icon name="alert-triangle" />
        {forbidden ? 'You don’t have access to the audit log' : 'Couldn’t load the audit log'}
      </p>
      <p className="text-foreground-muted">
        {forbidden
          ? 'The audit trail is restricted to admin and security roles.'
          : message}
      </p>
      {!forbidden ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted disabled:opacity-60"
        >
          {busy ? 'Retrying…' : 'Retry'}
        </button>
      ) : null}
    </div>
  );
}

function Pagination({
  canPrev,
  canNext,
  onPrev,
  onNext,
  page,
  count,
}: {
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  page: number;
  count: number;
}) {
  return (
    <nav
      aria-label="Audit pagination"
      className="flex shrink-0 items-center justify-between border-t border-border px-4 py-2 text-sm"
    >
      <span className="text-foreground-muted">
        Page {page}
        {count > 0 ? ` · ${count} event${count === 1 ? '' : 's'}` : ''}
      </span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onPrev}
          disabled={!canPrev}
          className="rounded-md border border-border px-3 py-1 hover:bg-surface-muted disabled:opacity-50"
        >
          Previous
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canNext}
          className="rounded-md border border-border px-3 py-1 hover:bg-surface-muted disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </nav>
  );
}
