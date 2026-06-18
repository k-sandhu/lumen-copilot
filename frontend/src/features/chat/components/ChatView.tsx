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
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api';
import type { ChatModelInfo, SendMessageRequest } from '@/api';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { useChatStore } from '../model/chatStore';
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

  const models = useModels();
  const queryClient = useQueryClient();

  return (
    <div className="flex h-full min-h-0">
      <aside className="hidden min-h-0 w-64 shrink-0 border-r border-border md:block">
        <ErrorBoundary label="Chat history">
          <HistorySidebar
            activeSessionId={activeSessionId}
            onSelect={openSession}
            onCreated={openSession}
            onDeleted={(id) => {
              if (id === activeSessionId) closeSession();
            }}
          />
        </ErrorBoundary>
      </aside>

      <main className="flex min-h-0 min-w-0 flex-1">
        {activeSessionId ? (
          <ErrorBoundary label="Conversation">
            <ActiveSession
              key={activeSessionId}
              sessionId={activeSessionId}
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
          <div className="flex min-h-0 flex-1 items-center justify-center p-8 text-center text-sm text-foreground-muted">
            <p>
              Pick a chat from the sidebar or start a new one to ask about your documents.
            </p>
          </div>
        )}

        {viewer && (
          <aside className="min-h-0 w-full max-w-[28rem] shrink-0 border-l border-border md:w-[28rem]">
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
                onClose={closeViewer}
              />
            </ErrorBoundary>
          </aside>
        )}
      </main>
    </div>
  );
}

interface ActiveSessionProps {
  sessionId: string;
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
  }) => void;
  onDoneReload: () => void;
}

function ActiveSession({
  sessionId,
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

  // The selected model: default until the user changes it (AC-3). The session
  // model is persisted via PATCH when changed.
  const [model, setModel] = useState<string>('');
  // Remember the last request so a stream error/disconnect can be retried (AC-5).
  const lastSendRef = useRef<SendMessageRequest | null>(null);

  // Preselect the default once models load (don't clobber a user choice).
  useEffect(() => {
    if (model === '' && models && models.length > 0) {
      setModel(defaultModelId(models));
    }
  }, [model, models]);

  // The assistant messageId we're waiting for the server reload to surface. The
  // live answer stays rendered until the persisted message arrives, so the turn
  // never flickers out between `done` and the GET .../messages refetch (AC-2).
  const [pendingDoneId, setPendingDoneId] = useState<string | null>(null);

  const stream = useChatStream({
    streamId: activeStreamId,
    onDone: () => onDoneReload(),
  });

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
        problem: stream.problem,
        model: stream.start?.model ?? model,
      }
    : null;

  const doSend = useCallback(
    (req: SendMessageRequest) => {
      lastSendRef.current = req;
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

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="min-h-0 flex-1">
        <ChatThread
          messages={messages.data?.items ?? []}
          isLoading={messages.isLoading}
          isError={messages.isError}
          error={messages.error}
          onRetryLoad={() => void messages.refetch()}
          live={live}
          onRetryStream={onRetryStream}
          onOpenCitation={(c) =>
            openViewer({
              documentId: c.documentId,
              documentName: c.documentName,
              charStart: c.charStart,
              charEnd: c.charEnd,
              snippet: c.snippet,
            })
          }
        />
      </div>

      <div className="shrink-0 border-t border-border p-3">
        {send.isError && (
          <p role="alert" className="mb-2 text-xs text-danger">
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
        />
      </div>
    </div>
  );
}
