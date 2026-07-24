/**
 * Server-state hooks for chat — TanStack Query (NOT a store; frontend/AGENTS.md:
 * server data never mirrored into Zustand). Sessions, messages, and usage are
 * server data; mutations (new/rename/delete/send) invalidate them.
 *
 * The shared model registry (GET /models) lives in `@/features/models`, not here:
 * preferences and assistants read it too, and routing them through the chat
 * barrel is what pulled the markdown pipeline onto the first-paint path (FE-9,
 * see `buildguards/chat-pipeline-not-in-entry.test.ts`).
 *
 * All reads can 404 when the resource is in another tenant or not permitted
 * (spec 0004 INV-1/INV-2); that surfaces as an ApiError the components branch on.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createChatSession,
  deleteChatSession,
  getSessionUsage,
  listChatSessions,
  listMessages,
  sendMessage,
  updateChatSession,
} from '@/api';
import { suggestSearch } from '@/api/suggest';
import type {
  ChatSession,
  ChatSessionCreate,
  ChatSessionList,
  ChatSessionUpdate,
  MessageList,
  SendMessageRequest,
  SendMessageResponse,
  SessionUsage,
  SuggestResponse,
} from '@/api';

export const chatKeys = {
  all: ['chat'] as const,
  sessions: () => [...chatKeys.all, 'sessions'] as const,
  messages: (sessionId: string) => [...chatKeys.all, 'messages', sessionId] as const,
  usage: (sessionId: string) => [...chatKeys.all, 'usage', sessionId] as const,
  mentionSuggest: (q: string) => [...chatKeys.all, 'mention-suggest', q] as const,
};

/**
 * The session's token/context accounting — the context meter (spec 0007 #432).
 * Invalidated on stream `done` (a new answer moved the numbers).
 */
export function useSessionUsage(sessionId: string | null) {
  return useQuery<SessionUsage>({
    queryKey: chatKeys.usage(sessionId ?? '∅'),
    queryFn: ({ signal }) => getSessionUsage(sessionId as string, signal),
    enabled: sessionId !== null,
    staleTime: 5_000,
  });
}

/**
 * Permission-trimmed document suggestions for the composer's @-mention picker
 * (spec 0007 #432). Reuses GET /search/suggest (the INV-2-trimmed surface —
 * a title the caller can't open is never suggested); the picker filters to
 * kind=document client-side. Failure degrades to "no matches" (AC-N2).
 */
export function useMentionSuggestions(query: string) {
  const trimmed = query.trim();
  return useQuery<SuggestResponse>({
    queryKey: chatKeys.mentionSuggest(trimmed),
    queryFn: ({ signal }) => suggestSearch(trimmed, 8, signal),
    enabled: trimmed.length > 0,
    staleTime: 30_000,
    retry: false,
  });
}

/** The chat-history sidebar list (AC-4). */
export function useChatSessions() {
  return useQuery<ChatSessionList>({
    queryKey: chatKeys.sessions(),
    queryFn: ({ signal }) => listChatSessions(undefined, signal),
    staleTime: 10_000,
  });
}

/** Message history for the open session (AC-2 reload-from-server). */
export function useMessages(sessionId: string | null) {
  return useQuery<MessageList>({
    queryKey: chatKeys.messages(sessionId ?? '∅'),
    queryFn: ({ signal }) => listMessages(sessionId as string, undefined, signal),
    enabled: sessionId !== null,
  });
}

/** Create a new chat session (AC-4 new). Invalidates the sidebar list. */
export function useCreateSession() {
  const qc = useQueryClient();
  return useMutation<ChatSession, unknown, ChatSessionCreate>({
    mutationFn: (body) => createChatSession(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: chatKeys.sessions() }),
  });
}

/** Rename a session / change its model (AC-4 rename, AC-3 per-session model). */
export function useUpdateSession() {
  const qc = useQueryClient();
  return useMutation<ChatSession, unknown, { sessionId: string; body: ChatSessionUpdate }>({
    mutationFn: ({ sessionId, body }) => updateChatSession(sessionId, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: chatKeys.sessions() }),
  });
}

/** Delete a session and its messages (AC-4 delete). */
export function useDeleteSession() {
  const qc = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (sessionId) => deleteChatSession(sessionId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: chatKeys.sessions() }),
  });
}

/**
 * Send a user message (AC-1). Returns the persisted user message + the
 * `stream_id` the caller subscribes to over WS. We optimistically nudge the
 * messages cache so the user's turn shows immediately; the authoritative
 * reload happens on stream `done`.
 */
export function useSendMessage(sessionId: string) {
  const qc = useQueryClient();
  return useMutation<SendMessageResponse, unknown, SendMessageRequest>({
    mutationFn: (body) => sendMessage(sessionId, body),
    onSuccess: (res) => {
      qc.setQueryData<MessageList>(chatKeys.messages(sessionId), (prev) => {
        const items = prev?.items ?? [];
        return { items: [...items, res.message], next_cursor: prev?.next_cursor ?? null };
      });
    },
  });
}
