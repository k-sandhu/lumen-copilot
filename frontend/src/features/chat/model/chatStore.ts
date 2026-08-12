/**
 * Chat UI state (Zustand) — ONLY genuine client-side state (frontend/AGENTS.md).
 * Server data (sessions, messages, models) lives in TanStack Query and is NEVER
 * mirrored here. This holds: which session is open, the in-flight answer stream
 * id, the per-turn model override, and the citation viewer target.
 */
import { create } from 'zustand';
import type { KnowledgeMode } from '@/api';

/**
 * What the citation viewer is currently showing (AC-2 click-through). It carries
 * only what the chat/citation wire actually provides about the source (its id,
 * name, cited span, and optional player-relative media time/speaker). It
 * deliberately holds no freshness/last-indexed label: neither a media offset nor
 * the answer/message time says when the source was indexed, so the viewer never
 * presents either as indexing provenance (#120 GUARD).
 */
export interface ViewerTarget {
  documentId: string;
  documentName: string;
  charStart: number;
  charEnd: number;
  snippet: string;
  timeStartMs?: number;
  timeEndMs?: number;
  transcriptSegmentId?: string;
  speakerId?: string;
  speakerName?: string;
  /**
   * Present when the citation is a web page (#221): its URL. Absent ⇒ a corpus
   * document. Drives the web variant of the inspector/viewer (globe + host +
   * "Open page") vs the document viewer.
   */
  url?: string;
}

/**
 * The knowledge-scope facts a session was started with (#221). The running
 * `ChatSession` wire does NOT carry the assistant's `knowledgeScope.modes` (nor
 * an `assistant_id`), so when a chat is launched FROM an assistant we stash its
 * modes here at launch time. It seeds the composer's knowledge-mode control so
 * the active modes are visible, and marks WEB available only when the assistant
 * scope includes it (governed — ADR-0014). An ad-hoc chat (no assistant) has no
 * scope and the composer falls back to its own defaults.
 */
export interface SessionScope {
  /** The session id this scope belongs to (guards against a stale scope). */
  sessionId: string;
  /** The assistant's declared knowledge modes; [] / undefined ⇒ none declared. */
  modes: KnowledgeMode[];
}

interface ChatUiState {
  /** The open session, or null when none is selected (empty state). */
  activeSessionId: string | null;
  /** The answer stream currently being consumed, or null when idle. */
  activeStreamId: string | null;
  /** Per-turn model override (picker); null = use the session default. */
  pendingModel: string | null;
  /** The citation the viewer pane is opened on, or null when closed. */
  viewer: ViewerTarget | null;
  /**
   * The knowledge scope the active session was started with (#221), or null for
   * an ad-hoc chat / a session opened without an assistant context. Cleared when
   * a different session opens so it never leaks across sessions.
   */
  sessionScope: SessionScope | null;
  /**
   * The open side panel (spec 0007 #432): the conversation's context summary
   * or its artifacts viewer. Mutually exclusive with the citation viewer (one
   * aside slot); null when closed.
   */
  panel: 'context' | 'artifacts' | null;

  /** Open a session, optionally seeding it with an assistant's knowledge modes. */
  openSession: (sessionId: string, scope?: KnowledgeMode[]) => void;
  closeSession: () => void;
  startStream: (streamId: string) => void;
  endStream: () => void;
  setPendingModel: (model: string | null) => void;
  openViewer: (target: ViewerTarget) => void;
  closeViewer: () => void;
  openPanel: (panel: 'context' | 'artifacts') => void;
  closePanel: () => void;
}

export const useChatStore = create<ChatUiState>((set) => ({
  activeSessionId: null,
  activeStreamId: null,
  pendingModel: null,
  viewer: null,
  sessionScope: null,
  panel: null,

  // Opening a session ends any in-flight stream and clears the viewer. When a
  // scope (the launching assistant's modes) is supplied, stash it against this
  // session; otherwise clear any prior scope so it never leaks to an ad-hoc chat.
  openSession: (sessionId, scope) =>
    set({
      activeSessionId: sessionId,
      activeStreamId: null,
      viewer: null,
      panel: null,
      sessionScope: scope ? { sessionId, modes: scope } : null,
    }),
  closeSession: () =>
    set({
      activeSessionId: null,
      activeStreamId: null,
      viewer: null,
      panel: null,
      sessionScope: null,
    }),
  startStream: (streamId) => set({ activeStreamId: streamId }),
  endStream: () => set({ activeStreamId: null }),
  setPendingModel: (model) => set({ pendingModel: model }),
  // Viewer and panel share the single aside slot — opening one closes the other.
  openViewer: (target) => set({ viewer: target, panel: null }),
  closeViewer: () => set({ viewer: null }),
  openPanel: (panel) => set({ panel, viewer: null }),
  closePanel: () => set({ panel: null }),
}));
