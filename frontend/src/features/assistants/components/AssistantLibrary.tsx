/**
 * AssistantLibrary (#212, AC-2/AC-3) — the `/assistants` screen body: a grid of
 * assistant cards with search/filter, a "New assistant" CTA, and the "Start chat"
 * flow that creates a chat session bound to the assistant and navigates to chat.
 *
 * Implements EVERY async state (frontend/AGENTS.md "every state, not just
 * success"):
 *   loading → a card skeleton grid
 *   error   → an actionable message with retry (401 messaged distinctly, INV-4)
 *   empty   → "Create your first assistant" with a primary CTA
 *   success → the searchable/filterable card grid
 *
 * "Start chat" is the critical flow (AC-2): POST /chat/sessions with the
 * assistant_id, then open that session in the chat store and navigate to `/`. A
 * 404/422 from create-session surfaces inline on the originating card (not a
 * crash — AC-5).
 */
import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, createChatSession } from '@/api';
import type { Assistant, AssistantStatus } from '@/api';
import { useChatStore } from '@/features/chat';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useMutation } from '@tanstack/react-query';
import { useAssistants, useMembers, useModels } from '../model/queries';
import { STATUS_LABEL } from '../model/presentation';
import { AssistantCard } from './AssistantCard';

type StatusFilter = 'all' | AssistantStatus;

const STATUS_FILTERS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'published', label: STATUS_LABEL.published },
  { value: 'draft', label: STATUS_LABEL.draft },
  { value: 'disabled', label: STATUS_LABEL.disabled },
];

export function AssistantLibrary() {
  const query = useAssistants();
  const models = useModels();
  const members = useMembers();
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');
  // Track which card started a chat + any per-card error, so only that card
  // shows its "Starting…" / inline failure.
  const [startingId, setStartingId] = useState<string | null>(null);
  const [startError, setStartError] = useState<{ id: string; message: string } | null>(null);

  const startChat = useMutation<{ id: string }, unknown, Assistant>({
    mutationFn: (assistant) => createChatSession({ assistant_id: assistant.id }),
  });

  const data = query.data?.items;
  const items = useMemo(() => data ?? [], [data]);
  const filtered = useMemo(() => filterAssistants(items, search, status), [items, search, status]);

  const handleStart = (assistant: Assistant) => {
    setStartingId(assistant.id);
    setStartError(null);
    startChat.mutate(assistant, {
      onSuccess: (session) => {
        // Bind the chat workspace to the new session, then navigate to it.
        useChatStore.getState().openSession(session.id);
        navigate('/');
      },
      onError: (error) =>
        setStartError({
          id: assistant.id,
          message: startChatErrorMessage(error),
        }),
      onSettled: () => setStartingId(null),
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">Assistants</h1>
          <p className="mt-0.5 text-sm text-foreground-muted">
            Saved, governed chat configurations — pick one to start a grounded chat, or build your
            own.
          </p>
        </div>
        <Link
          to="/assistants/new"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="plus" className="shrink-0" />
          New assistant
        </Link>
      </header>

      {/* Search + status filter — only meaningful once there are assistants. */}
      {query.isSuccess && items.length > 0 ? (
        <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-5 py-3">
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">Search assistants</span>
            <Icon
              name="search"
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-foreground-muted"
            />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, description, or owner"
              className="w-full rounded-md border border-border bg-surface py-1.5 pl-8 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent"
            />
          </label>
          <div className="flex items-center gap-1" role="group" aria-label="Filter by status">
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.value}
                type="button"
                aria-pressed={status === f.value}
                onClick={() => setStatus(f.value)}
                className={
                  status === f.value
                    ? 'rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white'
                    : 'rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted'
                }
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-5 py-5">
          <Body
            query={query}
            items={items}
            filtered={filtered}
            models={models.data?.items}
            members={members.data?.items}
            startingId={startingId}
            startError={startError}
            onStart={handleStart}
          />
        </ScrollArea>
      </div>
    </div>
  );
}

type AssistantsQuery = ReturnType<typeof useAssistants>;

/**
 * A user-facing message for a failed "Start chat". The lead is always the same
 * friendly guidance (a raw problem `title` like "Not Found" isn't actionable);
 * a server-supplied `detail` (e.g. "assistant is not published") is appended when
 * present so the user learns why.
 */
function startChatErrorMessage(error: unknown): string {
  const base = 'Could not start a chat with this assistant.';
  if (error instanceof ApiError && error.problem?.detail) {
    return `${base} ${error.problem.detail}`;
  }
  return base;
}

function filterAssistants(
  items: Assistant[],
  search: string,
  status: StatusFilter,
): Assistant[] {
  const q = search.trim().toLowerCase();
  return items.filter((a) => {
    if (status !== 'all' && a.status !== status) return false;
    if (q === '') return true;
    const haystack = `${a.name} ${a.description ?? ''} ${a.owner}`.toLowerCase();
    return haystack.includes(q);
  });
}

function Body({
  query,
  items,
  filtered,
  models,
  members,
  startingId,
  startError,
  onStart,
}: {
  query: AssistantsQuery;
  items: Assistant[];
  filtered: Assistant[];
  models: Parameters<typeof AssistantCard>[0]['models'];
  members: Parameters<typeof AssistantCard>[0]['members'];
  startingId: string | null;
  startError: { id: string; message: string } | null;
  onStart: (assistant: Assistant) => void;
}) {
  if (query.isPending) return <LoadingGrid />;
  if (query.isError) {
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} busy={query.isFetching} />
    );
  }
  if (items.length === 0) return <EmptyState />;
  if (filtered.length === 0) return <NoMatches />;

  return (
    <ul
      aria-label="Assistants"
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
    >
      {filtered.map((assistant) => (
        <li key={assistant.id} className="min-w-0">
          <AssistantCard
            assistant={assistant}
            models={models}
            members={members}
            starting={startingId === assistant.id}
            startDisabled={assistant.status === 'disabled'}
            startError={startError?.id === assistant.id ? startError.message : null}
            onStart={onStart}
          />
        </li>
      ))}
    </ul>
  );
}

function LoadingGrid() {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-live="polite"
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
    >
      <span className="sr-only">Loading assistants…</span>
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          className="h-56 rounded-lg border border-border bg-surface p-4"
          aria-hidden="true"
        >
          <div className="flex gap-3">
            <div className="lc-skeleton" style={{ width: 36, height: 36, borderRadius: 8 }} />
            <div className="grow space-y-2">
              <div className="lc-skeleton" style={{ width: '55%' }} />
              <div className="lc-skeleton" style={{ width: '80%' }} />
            </div>
          </div>
          <div className="lc-skeleton" style={{ width: '100%', marginTop: 24, height: 36 }} />
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border px-6 py-20 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-xl bg-surface-muted text-foreground-muted">
        <Icon name="sparkles" />
      </span>
      <div>
        <p className="text-sm font-medium">Create your first assistant</p>
        <p className="mx-auto mt-1 max-w-sm text-sm text-foreground-muted">
          Give it plain-language instructions, pick a model and the sources it can read, and share
          it with your team — no code required.
        </p>
      </div>
      <Link
        to="/assistants/new"
        className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Icon name="plus" className="shrink-0" />
        New assistant
      </Link>
    </div>
  );
}

function NoMatches() {
  return (
    <div className="rounded-xl border border-dashed border-border px-6 py-16 text-center text-sm text-foreground-muted">
      No assistants match your search or filter.
    </div>
  );
}

function ErrorState({
  error,
  onRetry,
  busy,
}: {
  error: unknown;
  onRetry: () => void;
  busy: boolean;
}) {
  // A 401 (expired/missing token, INV-4) is a re-auth dead-end a retry won't fix;
  // message it honestly. Everything else is transient — offer a retry.
  const status = error instanceof ApiError ? error.status : 0;
  const unauthorized = status === 401;
  const message = unauthorized
    ? 'Your session expired. Sign in again to see your assistants.'
    : error instanceof ApiError
      ? error.displayMessage || 'Could not load your assistants.'
      : 'Could not load your assistants.';

  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-danger/10 p-8 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="text-sm font-medium text-danger">Couldn’t load assistants</p>
      <p className="max-w-sm text-sm text-foreground-muted">{message}</p>
      {!unauthorized ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {busy ? 'Retrying…' : 'Retry'}
        </button>
      ) : null}
    </div>
  );
}
