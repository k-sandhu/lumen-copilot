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
import type { ChatModelInfo, KnowledgeMode, Message, SendMessageRequest } from '@/api';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Icon } from '@/ui';
import { usePreferences, useUpdatePreferences } from '@/features/preferences';
import { useModels } from '@/features/models';
import { useChatStore, type SessionScope } from '../model/chatStore';
import { initialModes, modeAvailability } from '../model/presentation';
import type { UiCitation } from '../model/citation';
import '../chat.css';
import { useMessages, useSendMessage, useUpdateSession } from '../model/queries';
import { useChatStream } from '../model/useChatStream';
import { useQueryClient } from '@tanstack/react-query';
import { chatKeys } from '../model/queries';
import { ArtifactPanel } from '@/features/artifacts';
import { HistorySidebar } from './HistorySidebar';
import { ChatThread, type LiveAnswer } from './ChatThread';
import { Composer, type PinnedDocument } from './Composer';
import { ContextMeter } from './ContextMeter';
import { ContextPanel } from './ContextPanel';
import { DocumentViewer } from './DocumentViewer';
import { WebSourceView } from './WebSourceView';
import { SandboxSessionControl } from './SandboxSessionControl';

function defaultModelId(models: ChatModelInfo[] | undefined): string {
  if (!models || models.length === 0) return '';
  return (models.find((m) => m.is_default) ?? models[0])?.id ?? '';
}

// Stable empty collections so a prop keeps its identity while its query loads (a
// fresh `[]` each render would defeat the memoised child it feeds, #495).
const EMPTY_MODELS: ChatModelInfo[] = [];
const EMPTY_MESSAGES: Message[] = [];

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
  const panel = useChatStore((s) => s.panel);
  const openPanel = useChatStore((s) => s.openPanel);
  const closePanel = useChatStore((s) => s.closePanel);
  // The artifact the artifacts pane focuses when opened from the context panel
  // (spec 0007 AC-3); null = the pane's own list default.
  const [artifactFocus, setArtifactFocus] = useState<string | null>(null);

  const models = useModels();
  const queryClient = useQueryClient();

  // Stable identity so nothing downstream of `ActiveSession` churns when this
  // component re-renders (#495 — the same rule the callbacks inside
  // `ActiveSession` follow). `queryClient` is stable for the provider's lifetime.
  const onDoneReload = useCallback(() => {
    if (!activeSessionId) return;
    void queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeSessionId) });
    // A settled answer moved the token accounting (spec 0007 AC-1).
    void queryClient.invalidateQueries({ queryKey: chatKeys.usage(activeSessionId) });
  }, [activeSessionId, queryClient]);

  // BE2-7: the follow-up suggestion is generated AFTER the terminal, so its
  // token spend lands after the `done` refresh above — the meter would otherwise
  // keep showing the pre-suggestion total until something else invalidated it.
  // Re-read usage once the suggestions window has settled (delivered or expired).
  const onSuggestionsSettled = useCallback(() => {
    if (!activeSessionId) return;
    void queryClient.invalidateQueries({ queryKey: chatKeys.usage(activeSessionId) });
  }, [activeSessionId, queryClient]);

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
                sessionScope && sessionScope.sessionId === activeSessionId ? sessionScope : null
              }
              models={models.data?.items}
              modelsLoading={models.isLoading}
              activeStreamId={activeStreamId}
              startStream={startStream}
              endStream={endStream}
              openViewer={openViewer}
              onDoneReload={onDoneReload}
              onSuggestionsSettled={onSuggestionsSettled}
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

      {/* Context / artifacts panes (spec 0007 #432) — same aside slot as the
          citation inspector (the store keeps them mutually exclusive). */}
      {panel && activeSessionId && (
        <button
          type="button"
          className="lc-chat__scrim"
          aria-label="Close panel"
          onClick={closePanel}
        />
      )}
      {panel === 'context' && activeSessionId && (
        <aside className="lc-chat__inspector">
          <ErrorBoundary label="Context panel">
            <ContextPanel
              sessionId={activeSessionId}
              onOpenCitation={(c) =>
                openViewer({
                  documentId: c.document_id,
                  documentName: c.document_name,
                  charStart: c.char_start,
                  charEnd: c.char_end,
                  snippet: c.snippet,
                })
              }
              onOpenArtifact={(id) => {
                setArtifactFocus(id);
                openPanel('artifacts');
              }}
              onClose={closePanel}
            />
          </ErrorBoundary>
        </aside>
      )}
      {panel === 'artifacts' && activeSessionId && (
        <aside className="lc-chat__inspector lc-chat__inspector--artifacts">
          <ErrorBoundary label="Artifacts">
            <div className="lc-ctxpanel__head">
              <h2 className="lc-ctxpanel__title" id="chat-artifacts-heading">
                <Icon name="package" />
                Conversation artifacts
              </h2>
              <button
                type="button"
                className="lc-ctxpanel__close"
                aria-label="Close artifacts"
                onClick={() => {
                  setArtifactFocus(null);
                  closePanel();
                }}
              >
                <Icon name="x" />
              </button>
            </div>
            <div className="lc-chat__artifactsbody">
              <ArtifactPanel
                sessionId={activeSessionId}
                headingId="chat-artifacts-heading"
                {...(artifactFocus ? { initialArtifactId: artifactFocus } : {})}
              />
            </div>
          </ErrorBoundary>
        </aside>
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
  /** Called once the post-terminal suggestions window settles (BE2-7). */
  onSuggestionsSettled: () => void;
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
  onSuggestionsSettled,
}: ActiveSessionProps) {
  const messages = useMessages(sessionId);
  const send = useSendMessage(sessionId);
  const updateSession = useUpdateSession();
  const prefs = usePreferences();
  const setDefaultPref = useUpdatePreferences();

  // #495 INVARIANT — every callback that reaches the persisted render path must
  // depend on the STABLE `mutate` function, never on the `useMutation` RESULT.
  // TanStack returns `{ ...result, mutate, mutateAsync }`: a brand-new wrapper
  // object on EVERY render, while `mutate`/`mutateAsync` keep their identity for
  // the observer's lifetime. Closing over the result object makes `doSend` — and
  // therefore `onSend`, which every `MessageBubble` receives as `onChooseOption`
  // — change on every streamed delta, which silently defeats `PersistedMessages`'
  // memo and re-renders the whole thread per token. Pinned by
  // `ChatViewRenderHygiene.test.tsx`, which drives deltas through this wiring.
  const sendMutate = send.mutate;
  const updateSessionMutate = updateSession.mutate;
  const setDefaultPrefMutate = setDefaultPref.mutate;

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
    if (model) setDefaultPrefMutate({ default_model_id: model });
  }, [model, setDefaultPrefMutate]);

  // The assistant messageId we're waiting for the server reload to surface. The
  // live answer stays rendered until the persisted message arrives, so the turn
  // never flickers out between `done` and the GET .../messages refetch (AC-2).
  const [pendingDoneId, setPendingDoneId] = useState<string | null>(null);

  // Follow-up suggestions for the latest settled answer (spec 0006 #429).
  // Captured OUT of the stream state so the chips survive the done→reload
  // stream retirement; cleared on the next send and on session switch.
  const [followUps, setFollowUps] = useState<string[]>([]);

  // Documents pinned into retrieval scope via @-mentions (spec 0007 #432).
  // Sticky for the conversation until unpinned; ActiveSession remounts per
  // session (key=sessionId), so pins never leak across sessions.
  const [pinnedDocs, setPinnedDocs] = useState<PinnedDocument[]>([]);
  const pinDocument = useCallback((doc: PinnedDocument) => {
    setPinnedDocs((current) =>
      current.some((d) => d.id === doc.id) ? current : [...current, doc],
    );
  }, []);
  const unpinDocument = useCallback((id: string) => {
    setPinnedDocs((current) => current.filter((d) => d.id !== id));
  }, []);

  const stream = useChatStream({
    streamId: activeStreamId,
    onDone: () => onDoneReload(),
  });

  useEffect(() => {
    // Promote suggestions only once the stream settled successfully (#434
    // review, finding 1): the event arrives before the backend transaction
    // commits, so a suggestions → terminal-error sequence must leave neither
    // chips nor ghost. An error clears anything already promoted.
    if (stream.phase === 'done' && stream.suggestions) {
      setFollowUps(stream.suggestions.suggestions);
    } else if (stream.phase === 'error') {
      setFollowUps([]);
    }
  }, [stream.phase, stream.suggestions]);

  useEffect(() => {
    setFollowUps([]);
  }, [sessionId]);

  // Capture the done messageId once the stream settles successfully.
  useEffect(() => {
    if (stream.phase === 'done' && stream.done) setPendingDoneId(stream.done.messageId);
  }, [stream.phase, stream.done]);

  // BE2-7 — persisted-bubble retirement and SOCKET lifetime are separate. The
  // live bubble is retired the moment the authoritative server copy arrives (as
  // before), but that must not end the subscription: a `done(pendingSuggestions)`
  // keeps the socket open for the trailing `event:suggestions` (#489), and the
  // reload reliably beats it. Calling `endStream()` here — as this effect used
  // to — unmounted `useChatStream` and closed the socket, so the suggestion was
  // dropped and the usage query cached the pre-suggestion total. This holds the
  // stream id (hence the hook) until the window settles; `retiredStreamId` is
  // compared against the ACTIVE id, so a new turn self-clears it.
  const [retiredStreamId, setRetiredStreamId] = useState<string | null>(null);
  const liveRetired = retiredStreamId !== null && retiredStreamId === activeStreamId;

  // When the server reload includes the persisted assistant message, retire the
  // live turn — the authoritative server copy now renders it.
  const reloadedIds = messages.data?.items;
  useEffect(() => {
    if (!pendingDoneId || !reloadedIds) return;
    if (reloadedIds.some((m) => m.id === pendingDoneId)) {
      setPendingDoneId(null);
      setRetiredStreamId(activeStreamId);
    }
  }, [pendingDoneId, reloadedIds, activeStreamId]);

  // Retire the SUBSCRIPTION only once the retired turn has nothing outstanding:
  // `awaitingSuggestions` is true exactly while the hook holds the socket open
  // for the post-terminal suggestion, and false again the instant it is
  // delivered, the grace elapses, or the stream is cancelled.
  const awaitingSuggestions = stream.awaitingSuggestions;
  useEffect(() => {
    if (!liveRetired || awaitingSuggestions) return;
    endStream();
  }, [liveRetired, awaitingSuggestions, endStream]);

  // The suggestion's own token spend is recorded after the terminal, so refresh
  // usage when the window closes (BE2-7) — the `done` refresh saw the pre-
  // suggestion total. Only fires for turns that actually awaited a suggestion.
  const awaitedSuggestionsRef = useRef(false);
  useEffect(() => {
    if (awaitingSuggestions) {
      awaitedSuggestionsRef.current = true;
      return;
    }
    if (!awaitedSuggestionsRef.current) return;
    awaitedSuggestionsRef.current = false;
    onSuggestionsSettled();
  }, [awaitingSuggestions, onSuggestionsSettled]);

  // Memoised so its identity only changes when a stream field actually changes
  // (#495). A stable `live` lets ChatThread's memoised subtrees skip re-rendering
  // when ActiveSession re-renders for a reason unrelated to the stream.
  const live: LiveAnswer | null = useMemo(
    () =>
      activeStreamId && !liveRetired
        ? {
            phase: stream.phase,
            text: stream.text,
            citations: stream.citations,
            tools: stream.tools,
            codeRuns: stream.codeRuns,
            steps: stream.steps,
            narration: stream.narration?.text ?? null,
            askUser: stream.askUser,
            problem: stream.problem,
            model: stream.start?.model ?? model,
          }
        : null,
    [
      activeStreamId,
      liveRetired,
      stream.phase,
      stream.text,
      stream.citations,
      stream.tools,
      stream.codeRuns,
      stream.steps,
      stream.narration,
      stream.askUser,
      stream.problem,
      stream.start,
      model,
    ],
  );

  const doSend = useCallback(
    (req: SendMessageRequest) => {
      lastSendRef.current = req;
      // A new question supersedes the previous turn's follow-ups (spec 0006).
      setFollowUps([]);
      sendMutate(req, {
        onSuccess: (res) => startStream(res.stream_id),
      });
    },
    [sendMutate, startStream],
  );

  const onSend = useCallback(
    (content: string) => {
      const req: SendMessageRequest = {
        content,
        ...(model ? { model } : {}),
        // Pinned documents ride every send while present (spec 0007 #432).
        ...(pinnedDocs.length > 0 ? { document_ids: pinnedDocs.map((d) => d.id) } : {}),
      };
      doSend(req);
    },
    [doSend, model, pinnedDocs],
  );

  const onRetryStream = useCallback(() => {
    if (lastSendRef.current) doSend(lastSendRef.current);
  }, [doSend]);

  // Depend on `stream.cancel` (stable across renders — useChatStream memoises it)
  // not the whole `stream` result (a fresh object every render), so `onStop` keeps
  // a stable identity per delta and does not defeat Composer's memo (#495).
  const cancelStream = stream.cancel;
  const onStop = useCallback(() => {
    cancelStream();
    endStream();
  }, [cancelStream, endStream]);

  // Opening a citation forwards only what the citation wire provides about the
  // source; the answer-time `meta` is NOT source provenance and is intentionally
  // not forwarded as freshness/last-indexed (#120). A web citation (#221)
  // forwards its `url` so the inspector opens the web-source pane. Stable
  // identity (useCallback) so it does not defeat every MessageBubble's memo (the
  // single highest-leverage prop on the render path, #495).
  const onOpenCitation = useCallback(
    (c: UiCitation) =>
      openViewer({
        documentId: c.documentId,
        documentName: c.documentName,
        charStart: c.charStart,
        charEnd: c.charEnd,
        snippet: c.snippet,
        ...(c.url ? { url: c.url } : {}),
      }),
    [openViewer],
  );

  const refetchMessages = messages.refetch;
  const onRetryLoad = useCallback(() => void refetchMessages(), [refetchMessages]);

  const onModelChange = useCallback(
    (next: string) => {
      setModel(next);
      // Persist as the session default (best-effort; per-turn override still set).
      updateSessionMutate({ sessionId, body: { model: next } });
    },
    [sessionId, updateSessionMutate],
  );

  const streaming = live?.phase === 'streaming';
  const busy = send.isPending || streaming === true;

  // Bash-style composer recall (spec 0006 AC-5): the caller's own previous
  // messages in this conversation, newest first, with consecutive duplicates
  // collapsed (readline convention — research pass) so Up never shows the
  // same entry twice in a row.
  const historyEntries = useMemo(() => {
    const newestFirst = (messages.data?.items ?? [])
      .filter((m) => m.role === 'user')
      .map((m) => m.content)
      .reverse();
    return newestFirst.filter((entry, i) => i === 0 || entry !== newestFirst[i - 1]);
  }, [messages.data]);

  const openPanel = useChatStore((s) => s.openPanel);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* Conversation toolbar (spec 0007): the context meter + panel toggles. */}
      <div className="lc-chat__toolbar">
        <ContextMeter sessionId={sessionId} />
        <SandboxSessionControl sessionId={sessionId} />
        <div className="lc-chat__toolbar-spacer" />
        <button
          type="button"
          className="lc-chat__toolbtn"
          onClick={() => openPanel('context')}
          aria-label="Open conversation context"
        >
          <Icon name="list" />
          Context
        </button>
        <button
          type="button"
          className="lc-chat__toolbtn"
          onClick={() => openPanel('artifacts')}
          aria-label="Open conversation artifacts"
        >
          <Icon name="package" />
          Artifacts
        </button>
      </div>
      <div className="min-h-0 flex-1">
        <ChatThread
          messages={messages.data?.items ?? EMPTY_MESSAGES}
          models={models}
          isLoading={messages.isLoading}
          isError={messages.isError}
          error={messages.error}
          onRetryLoad={onRetryLoad}
          live={live}
          onRetryStream={onRetryStream}
          onSendText={onSend}
          sendBusy={busy}
          suggestions={followUps}
          onOpenCitation={onOpenCitation}
        />
      </div>

      <div className="lc-composer-wrap">
        {send.isError && (
          <p role="alert" className="lc-composer mb-2 text-center text-xs text-danger">
            {send.error instanceof ApiError ? send.error.displayMessage : 'Could not send message.'}
          </p>
        )}
        <Composer
          models={models ?? EMPTY_MODELS}
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
          pinnedDocuments={pinnedDocs}
          onPinDocument={pinDocument}
          onUnpinDocument={unpinDocument}
        />
      </div>
    </div>
  );
}
