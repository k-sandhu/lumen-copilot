/**
 * Chat-history sidebar (AC-4): lists the caller's sessions and supports
 * new / rename / delete. Implements every state (frontend/AGENTS.md): loading,
 * empty, error (with retry), and the populated list. Selecting a session opens
 * it; the active session is highlighted. Rename is inline; delete confirms.
 */
import { useState } from 'react';
import { ApiError } from '@/api';
import type { ChatSession } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { cn } from '@/lib/cn';
import {
  useChatSessions,
  useCreateSession,
  useDeleteSession,
  useUpdateSession,
} from '../model/queries';

export interface HistorySidebarProps {
  activeSessionId: string | null;
  onSelect: (sessionId: string) => void;
  /** Called with the new session id after creation. */
  onCreated: (sessionId: string) => void;
  /** Called when the active session is deleted so the view can clear. */
  onDeleted: (sessionId: string) => void;
}

export function HistorySidebar({
  activeSessionId,
  onSelect,
  onCreated,
  onDeleted,
}: HistorySidebarProps) {
  const sessions = useChatSessions();
  const create = useCreateSession();

  function handleNew() {
    create.mutate(
      {},
      {
        onSuccess: (session) => onCreated(session.id),
      },
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-foreground-muted">
          Chats
        </h2>
        <button
          type="button"
          onClick={handleNew}
          disabled={create.isPending}
          className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-surface-muted disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {create.isPending ? 'Creating…' : '+ New'}
        </button>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="p-2">
          {sessions.isLoading && (
            <p role="status" className="px-2 py-4 text-xs text-foreground-muted">
              Loading chats…
            </p>
          )}

          {sessions.isError && (
            <div role="alert" className="px-2 py-4 text-xs">
              <p className="text-danger">
                {sessions.error instanceof ApiError
                  ? sessions.error.displayMessage
                  : 'Could not load chats.'}
              </p>
              <button
                type="button"
                onClick={() => void sessions.refetch()}
                className="mt-2 rounded-md border border-border px-2 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                Retry
              </button>
            </div>
          )}

          {sessions.isSuccess && sessions.data.items.length === 0 && (
            <p className="px-2 py-4 text-xs text-foreground-muted">
              No chats yet. Start one with “+ New”.
            </p>
          )}

          {sessions.isSuccess && sessions.data.items.length > 0 && (
            <ul className="flex flex-col gap-0.5" aria-label="Chat history">
              {sessions.data.items.map((session) => (
                <SessionRow
                  key={session.id}
                  session={session}
                  active={session.id === activeSessionId}
                  onSelect={() => onSelect(session.id)}
                  onDeleted={() => onDeleted(session.id)}
                />
              ))}
            </ul>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}

function SessionRow({
  session,
  active,
  onSelect,
  onDeleted,
}: {
  session: ChatSession;
  active: boolean;
  onSelect: () => void;
  onDeleted: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
  const [confirming, setConfirming] = useState(false);
  const rename = useUpdateSession();
  const remove = useDeleteSession();

  function commitRename() {
    const next = title.trim();
    if (next.length === 0 || next === session.title) {
      setEditing(false);
      setTitle(session.title);
      return;
    }
    rename.mutate(
      { sessionId: session.id, body: { title: next } },
      { onSettled: () => setEditing(false) },
    );
  }

  function commitDelete() {
    remove.mutate(session.id, { onSuccess: onDeleted });
  }

  if (editing) {
    return (
      <li>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            commitRename();
          }}
          className="flex items-center gap-1 px-1 py-0.5"
        >
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={commitRename}
            aria-label="Rename chat"
            className="min-w-0 flex-1 rounded border border-border bg-surface px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          />
        </form>
      </li>
    );
  }

  return (
    <li className="group relative">
      <div
        className={cn(
          'flex items-center gap-1 rounded-md',
          active ? 'bg-surface-muted' : 'hover:bg-surface-muted',
        )}
      >
        <button
          type="button"
          onClick={onSelect}
          aria-current={active ? 'true' : undefined}
          className="min-w-0 flex-1 truncate px-2 py-1.5 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {session.title || 'Untitled chat'}
        </button>
        <div className="flex shrink-0 items-center gap-0.5 pr-1 opacity-0 focus-within:opacity-100 group-hover:opacity-100">
          <button
            type="button"
            onClick={() => {
              setTitle(session.title);
              setEditing(true);
            }}
            aria-label={`Rename ${session.title || 'chat'}`}
            className="rounded px-1 py-0.5 text-xs text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Rename
          </button>
          <button
            type="button"
            onClick={() => setConfirming(true)}
            aria-label={`Delete ${session.title || 'chat'}`}
            className="rounded px-1 py-0.5 text-xs text-foreground-muted hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Delete
          </button>
        </div>
      </div>

      {confirming && (
        <div
          role="alertdialog"
          aria-label="Confirm delete"
          className="mt-1 rounded-md border border-danger/40 bg-danger/10 p-2 text-xs"
        >
          <p>Delete this chat and its messages?</p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={commitDelete}
              disabled={remove.isPending}
              className="rounded-md border border-danger/60 px-2 py-1 text-danger hover:bg-danger/20 disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger"
            >
              {remove.isPending ? 'Deleting…' : 'Delete'}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-md border border-border px-2 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
