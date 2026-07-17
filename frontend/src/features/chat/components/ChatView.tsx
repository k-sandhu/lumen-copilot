/**
 * Chat feature root (issue #50) — orchestrates the whole slice on top of the #48
 * app shell: history sidebar, conversation thread, composer + model picker, the
 * live answer stream, and the citation document viewer. It owns the wiring; the
 * pieces are presentational/hook-driven and individually tested.
 *
 * Flow (AC-1/2/3): pick or create a session → choose a model → send a message
 * (POST .../messages) → subscribe to the returned stream_id over WS → render
 * delta/tool/citation live → on `done`, reload messages from the server. AC-4:
 * the sidebar lists/creates/renames/deletes sessions. AC-5: a WS error or
 * disconnect is terminal with a Retry; a zero-citation answer is shown honestly.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError } from '@/api';
import type { ChatModelInfo, KnowledgeMode, SendMessageRequest } from '@/api';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Icon } from '@/ui';
import { usePreferences, useUpdatePreferences } from '@/features/preferences';
import { useChatStore, type SessionScope } from '../model/chatStore';
import { initialModes, modeAvailability } from '../model/presentation';
import '../chat.css';
import {
  useMessages,
  useModels,
  useSendMessage,
  useUpdateSession,
} from '../model/queries';
import { useChatStream } from '../model/useChatStream';
import { useQueryClient } from '@tanstack/react-query';
import { chatKeys } from '../model/queries';
import { HistorySidebar } from './HistorySidebar';
import { ChatThread, type LiveAnswer } from './ChatThread';
import { Composer } from './Composer';
import { DocumentViewer } from './DocumentViewer';
import { WebSourceView } from './WebSourceView';

function defaultModelId(models: ChatModelInfo[] | undefined): string {
  if (!models || models.length === 0) return '';
  return (models.find((m) => m.is_default) ?? models[0])?.id ?? '';
}

export function ChatView() {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const openSession = useChatStore((s) => s.openSession);
  const closeSession = useChatStore((s) => s.closeSession);
  const activeStreamId = useChatStore((s) => s.activeStreamId);
  const startStream = useChatStore((s) => s.startStream);
  const endStream = useChatStore((s) => s.endStream);
  const viewer = useChatStore((s) => s.viewer);
  const openViewer = useChatStore((s) => s.openViewer);
  const closeViewer = useChatStore((s) => s.closeViewer);
  const sessionScope = useChatStore((s) => s.sessionScope);

  const models = useModels();
  const queryClient = useQueryClient();

  // At narrow widths the subrail is an off-canvas drawer (see chat.css); this
  // drives it. Selecting/creating a session closes the drawer so the narrow-width
  // flow lands back on the conversation.
  const [historyOpen, setHistoryOpen] = useState(false);
  const openAndClose = useCallback(
    (id: string) => {
      openSession(id);
      setHistoryOpen(false);
    },
    [openSession],
  );

  return (
    <div className="lc-chat">
      <aside className="lc-chat__subrail" data-open={historyOpen ? 'true' : 'false'}>
        <ErrorBoundary label="Chat history">
          <HistorySidebar
            activeSessionId={activeSessionId}
            onSelect={openAndClose}
            onCreated={openAndClose}
            onDeleted={(id) => {
              if (id === activeSessionId) closeSession();
            }}
          />
        </ErrorBoundary>
      </aside>

      {historyOpen && (
        <button
          type="button"
          className="lc-chat__subrail-scrim"
          aria-label="Close chat history"
          onClick={() => setHistoryOpen(false)}
        />
      )}

      <div className="lc-chat__main">
        {/* Narrow-width only (hidden on desktop via chat.css): opens the history
            drawer so chats stay reachable when the subrail is off-canvas. */}
        <div className="lc-chat__mobilebar">
          <button
            type="button"
            className="lc-chat__historytrigger"
            aria-label="Open chat history"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen(true)}
          >
            <Icon name="message-square" />
            Chats
          </button>
        </div>

        {activeSessionId ? (
          <ErrorBoundary label="Conversation">
            <ActiveSession
              key={activeSessionId}
              sessionId={activeSessionId}
              scope={
                sessionScope && sessionScope.sessionId === activeSessionId
                  ? sessionScope
                  : null
              }
              models={models.data?.items}
              modelsLoading={models.isLoading}
              activeStreamId={activeStreamId}
              startStream={startStream}
              endStream={endStream}
              openViewer={openViewer}
              onDoneReload={() =>
                void queryClient.invalidateQueries({
                  queryKey: chatKeys.messages(activeSessionId),
                })
              }
            />
          </ErrorBoundary>
        ) : (
          <div className="lc-chat-empty">
            <div className="lc-chat-empty__logo" aria-hidden="true">
              <Icon name="sparkles" />
            </div>
            <h2 className="lc-chat-empty__title">Ask across your work</h2>
            <p className="lc-chat-empty__sub">
              Pick a chat from the sidebar or start a new one. Every answer is grounded in your
              permitted sources and cited so you can verify it.
            </p>
          </div>
        )}
      </div>

      {viewer && (
        <button
          type="button"
          className="lc-chat__scrim"
          aria-label="Close source"
          onClick={closeViewer}
        />
      )}

      {viewer && (
        <aside className="lc-chat__inspector">
          {viewer.url ? (
            // A web citation (#221): render the web-source pane (host + snippet +
            // safe external link), NOT the document-bytes viewer — a web result
            // has no corpus document to fetch (INV-3).
            <ErrorBoundary label="Web source">
              <WebSourceView
                citation={{
                  id: `${viewer.url}:${viewer.charStart}`,
                  documentId: viewer.documentId,
                  documentName: viewer.documentName,
                  chunkId: '',
                  snippet: viewer.snippet,
                  charStart: viewer.charStart,
                  charEnd: viewer.charEnd,
                  url: viewer.url,
                }}
                onClose={closeViewer}
              />
            </ErrorBoundary>
          ) : (
            <ErrorBoundary label="Document viewer">
              <DocumentViewer
                citation={{
                  id: `${viewer.documentId}:${viewer.charStart}`,
                  documentId: viewer.documentId,
                  documentName: viewer.documentName,
                  chunkId: '',
                  snippet: viewer.snippet,
                  charStart: viewer.charStart,
                  charEnd: viewer.charEnd,
                }}
                // No source owner / last-modified / last-indexed is on the chat
                // wire, and the answer/message time is the answer's age, not the
                // source's — so the viewer shows "Not available" rather than
                // present the answer time as source provenance (#120 GUARD).
                onClose={closeViewer}
              />
            </ErrorBoundary>
          )}
        </aside>
      )}
    </div>
  );
}

interface ActiveSessionProps {
  sessionId: string;
  /** The knowledge scope this session was launched with (#221), or null. */
  scope: SessionScope | null;
  models: ChatModelInfo[] | undefined;
  modelsLoading: boolean;
  activeStreamId: string | null;
  startStream: (streamId: string) => void;
  endStream: () => void;
  openViewer: (target: {
    documentId: string;
    documentName: string;
    charStart: number;
    charEnd: number;
    snippet: string;
    url?: string;
  }) => void;
  onDoneReload: () => void;
}

function ActiveSession({
  sessionId,
  scope,
  models,
  modelsLoading,
  activeStreamId,
  startStream,
  endStream,
  openViewer,
  onDoneReload,
}: ActiveSessionProps) {
  const messages = useMessages(sessionId);
  const send = useSendMessage(sessionId);
  const updateSession = useUpdateSession();
  const prefs = usePreferences();
  const setDefaultPref = useUpdatePreferences();

  // The selected model: default until the user changes it (AC-3). The session
  // model is persisted via PATCH when changed.
  const [model, setModel] = useState<string>('');

  // The active knowledge modes for the next turn (#221). Seeded from the
  // launching assistant's scope (or the ad-hoc default), then a per-chat
  // override the user can toggle. WEB is available only when the scope allowed
  // it — otherwise the composer renders it disabled with a reason (AC-3).
  const scopeModes = scope?.modes;
  const [modes, setModes] = useState<KnowledgeMode[]>(() => initialModes(scopeModes));
  const availability = useMemo(() => modeAvailability(scopeModes), [scopeModes]);
  // Remember the last request so a stream error/disconnect can be retried (AC-5).
  const lastSendRef = useRef<SendMessageRequest | null>(null);

  // Preselect once models load (don't clobber a user choice): prefer the caller's
  // saved default-model preference (spec 0005) when it's still a valid registry
  // id, else the server default. Wait for the preference to settle so we don't
  // preselect the server default and then visibly jump.
  useEffect(() => {
    if (model !== '' || !models || models.length === 0 || prefs.isLoading) return;
    const preferred = prefs.data?.default_model_id;
    const valid = preferred != null && models.some((m) => m.id === preferred);
    setModel(valid ? preferred : defaultModelId(models));
  }, [model, models, prefs.isLoading, prefs.data]);

  // Persist the currently-selected model as the caller's default for new chats.
  const onSetDefaultModel = useCallback(() => {
    if (model) setDefaultPref.mutate({ default_model_id: model });
  }, [model, setDefaultPref]);

  // The assistant messageId we're waiting for the server reload to surface. The
  // live answer stays rendered until the persisted message arrives, so the turn
  // never flickers out between `done` and the GET .../messages refetch (AC-2).
  const [pendingDoneId, setPendingDoneId] = useState<string | null>(null);

  // Follow-up suggestions for the latest settled answer (spec 0006 #429).
  // Captured OUT of the stream state so the chips survive the done→reload
  // stream retirement; cleared on the next send and on session switch.
  const [followUps, setFollowUps] = useState<string[]>([]);

  const stream = useChatStream({
    streamId: activeStreamId,
    onDone: () => onDoneReload(),
  });

  useEffect(() => {
    if (stream.suggestions) setFollowUps(stream.suggestions.suggestions);
  }, [stream.suggestions]);

  useEffect(() => {
    setFollowUps([]);
  }, [sessionId]);

  // Capture the done messageId once the stream settles successfully.
  useEffect(() => {
    if (stream.phase === 'done' && stream.done) setPendingDoneId(stream.done.messageId);
  }, [stream.phase, stream.done]);

  // When the server reload includes the persisted assistant message, retire the
  // live stream — the authoritative server copy now renders it.
  const reloadedIds = messages.data?.items;
  useEffect(() => {
    if (!pendingDoneId || !reloadedIds) return;
    if (reloadedIds.some((m) => m.id === pendingDoneId)) {
      setPendingDoneId(null);
      endStream();
    }
  }, [pendingDoneId, reloadedIds, endStream]);

  const live: LiveAnswer | null = activeStreamId
    ? {
        phase: stream.phase,
        text: stream.text,
        citations: stream.citations,
        tools: stream.tools,
        codeRuns: stream.codeRuns,
        steps: stream.steps,
        askUser: stream.askUser,
        problem: stream.problem,
        model: stream.start?.model ?? model,
      }
    : null;

  const doSend = useCallback(
    (req: SendMessageRequest) => {
      lastSendRef.current = req;
      // A new question supersedes the previous turn's follow-ups (spec 0006).
      setFollowUps([]);
      send.mutate(req, {
        onSuccess: (res) => startStream(res.stream_id),
      });
    },
    [send, startStream],
  );

  const onSend = useCallback(
    (content: string) => {
      const req: SendMessageRequest = model ? { content, model } : { content };
      doSend(req);
    },
    [doSend, model],
  );

  const onRetryStream = useCallback(() => {
    if (lastSendRef.current) doSend(lastSendRef.current);
  }, [doSend]);

  const onStop = useCallback(() => {
    stream.cancel();
    endStream();
  }, [stream, endStream]);

  const onModelChange = useCallback(
    (next: string) => {
      setModel(next);
      // Persist as the session default (best-effort; per-turn override still set).
      updateSession.mutate({ sessionId, body: { model: next } });
    },
    [sessionId, updateSession],
  );

  const streaming = live?.phase === 'streaming';
  const busy = send.isPending || streaming === true;

  // Bash-style composer recall (spec 0006 AC-5): the caller's own previous
  // messages in this conversation, newest first.
  const historyEntries = useMemo(
    () =>
      (messages.data?.items ?? [])
        .filter((m) => m.role === 'user')
        .map((m) => m.content)
        .reverse(),
    [messages.data],
  );

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="min-h-0 flex-1">
        <ChatThread
          messages={messages.data?.items ?? []}
          models={models}
          isLoading={messages.isLoading}
          isError={messages.isError}
          error={messages.error}
          onRetryLoad={() => void messages.refetch()}
          live={live}
          onRetryStream={onRetryStream}
          onSendText={onSend}
          sendBusy={busy}
          suggestions={followUps}
          onOpenCitation={(c) =>
            // The viewer carries only what the citation wire provides about the
            // source; the answer-time `meta` is NOT a source-provenance signal
            // and is intentionally not forwarded as freshness/last-indexed (#120).
            // A web citation (#221) forwards its `url` so the inspector opens the
            // web-source pane instead of trying to fetch corpus bytes.
            openViewer({
              documentId: c.documentId,
              documentName: c.documentName,
              charStart: c.charStart,
              charEnd: c.charEnd,
              snippet: c.snippet,
              ...(c.url ? { url: c.url } : {}),
            })
          }
        />
      </div>

      <div className="lc-composer-wrap">
        {send.isError && (
          <p role="alert" className="lc-composer mb-2 text-center text-xs text-danger">
            {send.error instanceof ApiError ? send.error.displayMessage : 'Could not send message.'}
          </p>
        )}
        <Composer
          models={models ?? []}
          model={model}
          onModelChange={onModelChange}
          busy={busy}
          streaming={streaming === true}
          onSend={onSend}
          onStop={onStop}
          disabled={modelsLoading && (models?.length ?? 0) === 0}
          defaultModelId={prefs.data?.default_model_id ?? null}
          onSetDefaultModel={onSetDefaultModel}
          settingDefault={setDefaultPref.isPending}
          modes={modes}
          onModesChange={setModes}
          modeAvailability={availability}
          ghostSuggestion={!busy ? (followUps[0] ?? null) : null}
          historyEntries={historyEntries}
        />
      </div>
    </div>
  );
}
