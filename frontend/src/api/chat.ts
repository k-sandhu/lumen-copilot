/**
 * Typed chat calls — part of the api/ boundary (ADR-0004). The ONLY place the
 * SPA performs chat HTTP. Conforms to the frozen contract (contracts/openapi.yaml
 * §chat). The assistant answer itself rides the WebSocket envelope stream
 * (api/ws.ts + features/chat); these are the REST request/response calls:
 *
 *   GET    /chat/sessions                     → ChatSessionList (history sidebar)
 *   POST   /chat/sessions                     → ChatSession      (new chat)
 *   GET    /chat/sessions/{id}                → ChatSession
 *   PATCH  /chat/sessions/{id}                → ChatSession       (rename / model)
 *   DELETE /chat/sessions/{id}                → 204               (delete)
 *   GET    /chat/sessions/{id}/messages       → MessageList       (reload history)
 *   POST   /chat/sessions/{id}/messages       → SendMessageResponse (+ stream_id)
 *
 * Tenancy/permission (spec 0004): every resource is implicitly scoped to the
 * caller's tenant + ownership; a cross-tenant / not-permitted id returns 404
 * (INV-1/INV-2), surfaced as an ApiError the caller branches on.
 */
import { request } from './client';
import type {
  ChatSession,
  ChatSessionCreate,
  ChatSessionList,
  ChatSessionUpdate,
  MessageList,
  SendMessageRequest,
  SendMessageResponse,
} from './types';

export interface PageParams {
  cursor?: string;
  limit?: number;
}

function pageQuery(params?: PageParams): string {
  if (!params) return '';
  const q = new URLSearchParams();
  if (params.cursor) q.set('cursor', params.cursor);
  if (params.limit !== undefined) q.set('limit', String(params.limit));
  const s = q.toString();
  return s ? `?${s}` : '';
}

/** List the caller's chat sessions (history sidebar). */
export function listChatSessions(
  params?: PageParams,
  signal?: AbortSignal,
): Promise<ChatSessionList> {
  return request<ChatSessionList>(`/chat/sessions${pageQuery(params)}`, { signal });
}

/** Start a new chat session. */
export function createChatSession(body: ChatSessionCreate): Promise<ChatSession> {
  return request<ChatSession>('/chat/sessions', { method: 'POST', json: body });
}

/** Get a chat session by id (404 if not yours / cross-tenant — INV-1/INV-2). */
export function getChatSession(sessionId: string, signal?: AbortSignal): Promise<ChatSession> {
  return request<ChatSession>(`/chat/sessions/${sessionId}`, { signal });
}

/** Rename a session or change its default model. */
export function updateChatSession(
  sessionId: string,
  body: ChatSessionUpdate,
): Promise<ChatSession> {
  return request<ChatSession>(`/chat/sessions/${sessionId}`, { method: 'PATCH', json: body });
}

/** Delete a chat session and its messages. */
export function deleteChatSession(sessionId: string): Promise<void> {
  return request<void>(`/chat/sessions/${sessionId}`, { method: 'DELETE' });
}

/** Message history for a session (oldest → newest). */
export function listMessages(
  sessionId: string,
  params?: PageParams,
  signal?: AbortSignal,
): Promise<MessageList> {
  return request<MessageList>(`/chat/sessions/${sessionId}/messages${pageQuery(params)}`, {
    signal,
  });
}

/**
 * Send a user message. The persisted user message + a `stream_id` come back
 * (202); the caller then subscribes to that id on the WS envelope stream for the
 * assistant answer. `model` overrides the session default for this turn;
 * `collection_ids` scopes retrieval (omitted = all permitted docs).
 */
export function sendMessage(
  sessionId: string,
  body: SendMessageRequest,
): Promise<SendMessageResponse> {
  return request<SendMessageResponse>(`/chat/sessions/${sessionId}/messages`, {
    method: 'POST',
    json: body,
  });
}
