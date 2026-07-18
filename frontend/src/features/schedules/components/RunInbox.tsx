/**
 * RunInbox (#238, AC-1/AC-2/AC-3) — the in-app run inbox: a completed run's
 * deliveries newest first, each linking to the full cited run (#237). An "Unread
 * only" toggle drives the unread view; every delivery can be marked read. Every
 * async state is handled (loading skeletons / empty / error+retry). A `failed`
 * delivery is flagged inline so the negative case (AC-3) is never a blank/quiet row.
 * Scrolls independently inside a min-h-0 flex column.
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useMarkDeliveryRead, useRunDeliveries } from '../model/queries';
import { DeliveryRow } from './DeliveryRow';
import { EmptyState, ErrorState, Pagination, SkeletonRows } from './StateViews';

export function RunInbox() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined]);

  const cursor = cursorStack[cursorStack.length - 1];
  const query = useMemo(() => ({ unread: unreadOnly || undefined, cursor }), [unreadOnly, cursor]);

  const deliveries = useRunDeliveries(query);
  const markRead = useMarkDeliveryRead();

  const resetPaging = () => setCursorStack([undefined]);
  const nextPage = () => {
    const next = deliveries.data?.next_cursor;
    if (next) setCursorStack((s) => [...s, next]);
  };
  const prevPage = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-3 border-b border-border px-4 pb-4 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-foreground-muted">
            Completed runs delivered to you — open one for its full cited transcript.
          </p>
          <Link
            to="/runs"
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Icon name="calendar" className="shrink-0" />
            Run history
          </Link>
        </div>

        <label className="flex w-fit items-center gap-2 text-sm text-foreground-muted">
          <input
            type="checkbox"
            checked={unreadOnly}
            onChange={(e) => {
              setUnreadOnly(e.target.checked);
              resetPaging();
            }}
            className="rounded border-border"
          />
          Unread only
        </label>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-4 py-3">
          <InboxBody
            query={deliveries}
            unreadOnly={unreadOnly}
            onMarkRead={(id) => markRead.mutate(id)}
            markingRead={markRead.isPending}
          />
        </ScrollArea>
      </div>

      <Pagination
        label="Inbox pagination"
        page={cursorStack.length}
        canPrev={cursorStack.length > 1}
        canNext={Boolean(deliveries.data?.next_cursor)}
        onPrev={prevPage}
        onNext={nextPage}
      />
    </div>
  );
}

type DeliveriesQuery = ReturnType<typeof useRunDeliveries>;

function InboxBody({
  query,
  unreadOnly,
  onMarkRead,
  markingRead,
}: {
  query: DeliveriesQuery;
  unreadOnly: boolean;
  onMarkRead: (id: string) => void;
  markingRead: boolean;
}) {
  if (query.isPending) return <SkeletonRows label="Loading your inbox…" />;
  if (query.isError) {
    return (
      <ErrorState
        title="Couldn’t load your inbox"
        error={query.error}
        onRetry={() => void query.refetch()}
        busy={query.isFetching}
      />
    );
  }
  if (query.data.items.length === 0) {
    return unreadOnly ? (
      <EmptyState
        title="You’re all caught up"
        message="No unread run deliveries. Uncheck “Unread only” to see everything."
      />
    ) : (
      <EmptyState
        title="No run deliveries yet"
        message="When a scheduled or manual run completes, its result lands here."
      />
    );
  }
  return (
    <ul aria-label="Run deliveries" className="space-y-3">
      {query.data.items.map((delivery) => (
        <li key={delivery.id}>
          <DeliveryRow delivery={delivery} onMarkRead={onMarkRead} markingRead={markingRead} />
        </li>
      ))}
    </ul>
  );
}
