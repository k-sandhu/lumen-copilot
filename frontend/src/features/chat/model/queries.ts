/**
 * Server-state hooks for chat — TanStack Query (NOT a store; frontend/AGENTS.md:
 * server data never mirrored into Zustand). Sessions, messages, and the model
 * registry are server data; mutations (new/rename/delete/send) invalidate them.
 *
 * All reads can 404 when the resource is in another tenant or not permitted
 * (spec 0004 INV-1/INV-2); that surfaces as an ApiError the components branch on.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createChatSession,
  deleteChatSession,
  listChatSessions,
  listMessages,
  listModels,
  sendMessage,
  updateChatSession,
} from '@/api';
import type {
  ChatSession,
  ChatSessionCreate,
  ChatSessionList,
  ChatSessionUpdate,
  MessageList,
  ModelList,
  SendMessageRequest,
  SendMessageResponse,
} from '@/api';

export const chatKeys = {
  all: ['chat'] as const,
  sessions: () => [...chatKeys.all, 'sessions'] as const,
  messages: (sessionId: string) => [...chatKeys.all, 'messages', sessionId] as const,
  models: () => ['models'] as const,
};

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

/** The model-picker registry (AC-3). Stable for the session; cache generously. */
export function useModels() {
  return useQuery<ModelList>({
    queryKey: chatKeys.models(),
    queryFn: ({ signal }) => listModels(signal),
    staleTime: 5 * 60_000,
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
