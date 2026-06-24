/**
 * Chat-history sidebar (AC-4) — re-skinned to the canonical design (issue #136,
 * docs/wireframes/chat.html subrail): a "New chat" primary action, a client-side
 * search filter, sessions grouped by recency (Today / Yesterday / …) each with an
 * honest meta line ("N messages · model · updated"), and an elevated active card.
 *
 * Implements every state (frontend/AGENTS.md): loading, empty, error (with
 * retry), no-search-match, and the populated list. Selecting a session opens it;
 * the active session is highlighted. Rename is inline; delete confirms. Only
 * fields the session wire carries drive the meta line — no fabricated counts.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError } from '@/api';
import type { ChatSession } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';
import { groupSessionsByDay, sessionMeta } from '../model/presentation';
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
  const [query, setQuery] = useState('');

  function handleNew() {
    create.mutate(
      {},
      {
        onSuccess: (session) => onCreated(session.id),
      },
    );
  }

  const items = sessions.data?.items;
  const total = items?.length ?? 0;
  const filtered = useMemo(() => {
    const list = items ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter((s) => (s.title || 'Untitled chat').toLowerCase().includes(q));
  }, [items, query]);
  const groups = useMemo(() => groupSessionsByDay(filtered), [filtered]);

  return (
    <div className="lc-subrail">
      <div className="lc-subrail__head">
        <button
          type="button"
          onClick={handleNew}
          disabled={create.isPending}
          className="lc-newchat"
        >
          <Icon name="plus" />
          {create.isPending ? 'Creating…' : 'New chat'}
        </button>
        <div className="lc-field">
          <Icon name="search" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats"
            aria-label="Search chats"
            className="lc-field__input"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="lc-subrail__scroll-pad">
          {sessions.isLoading && (
            <p role="status" className="lc-subrail__state">
              Loading chats…
            </p>
          )}

          {sessions.isError && (
            <div role="alert" className="lc-subrail__state lc-subrail__state--error">
              <p>
                {sessions.error instanceof ApiError
                  ? sessions.error.displayMessage
                  : 'Could not load chats.'}
              </p>
              <button
                type="button"
                onClick={() => void sessions.refetch()}
                className="lc-confirm__btn mt-2"
              >
                Retry
              </button>
            </div>
          )}

          {sessions.isSuccess && total === 0 && (
            <p className="lc-subrail__state">No chats yet. Start one with “New chat”.</p>
          )}

          {sessions.isSuccess && total > 0 && filtered.length === 0 && (
            <p className="lc-subrail__state">No chats match “{query.trim()}”.</p>
          )}

          {groups.length > 0 && (
            <ul aria-label="Chat history">
              {groups.map((group) => (
                <li key={group.label}>
                  <div className="lc-histday">{group.label}</div>
                  <ul>
                    {group.sessions.map((session) => (
                      <SessionRow
                        key={session.id}
                        session={session}
                        active={session.id === activeSessionId}
                        onSelect={() => onSelect(session.id)}
                        onDeleted={() => onDeleted(session.id)}
                      />
                    ))}
                  </ul>
                </li>
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
  // The delete confirm is role="alertdialog": move focus into it on open, close on
  // Escape, and restore focus to the trigger on cancel (a11y — the role promises
  // dialog-like behavior).
  const deleteBtnRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  function cancelDelete() {
    setConfirming(false);
    deleteBtnRef.current?.focus();
  }

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

  const label = session.title || 'Untitled chat';

  if (editing) {
    return (
      <li>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            commitRename();
          }}
          className="lc-histrename"
        >
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={commitRename}
            aria-label="Rename chat"
            className="lc-histrename__input"
          />
        </form>
      </li>
    );
  }

  return (
    <li>
      <div className={cn('lc-histitem', active && 'is-active')}>
        <button
          type="button"
          onClick={onSelect}
          aria-current={active ? 'true' : undefined}
          aria-label={label}
          className="lc-histitem__open"
        >
          <div className="lc-histitem__title">{label}</div>
          <div className="lc-histitem__meta">{sessionMeta(session)}</div>
        </button>
        <div className="lc-histitem__actions">
          <button
            type="button"
            onClick={() => {
              setTitle(session.title);
              setEditing(true);
            }}
            aria-label={`Rename ${label}`}
            className="lc-histitem__action"
          >
            <Icon name="pencil" />
          </button>
          <button
            ref={deleteBtnRef}
            type="button"
            onClick={() => setConfirming(true)}
            aria-label={`Delete ${label}`}
            className="lc-histitem__action lc-histitem__action--danger"
          >
            <Icon name="trash" />
          </button>
        </div>
      </div>

      {confirming && (
        <div
          ref={confirmRef}
          role="alertdialog"
          aria-label="Confirm delete"
          tabIndex={-1}
          onKeyDown={(e) => {
            if (e.key === 'Escape') cancelDelete();
          }}
          className="lc-confirm"
        >
          <p>Delete this chat and its messages?</p>
          <div className="lc-confirm__actions">
            <button
              type="button"
              onClick={commitDelete}
              disabled={remove.isPending}
              className="lc-confirm__btn lc-confirm__btn--danger"
            >
              {remove.isPending ? 'Deleting…' : 'Delete'}
            </button>
            <button type="button" onClick={cancelDelete} className="lc-confirm__btn">
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
