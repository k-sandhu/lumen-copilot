/**
 * Chat UI state (Zustand) — ONLY genuine client-side state (frontend/AGENTS.md).
 * Server data (sessions, messages, models) lives in TanStack Query and is NEVER
 * mirrored here. This holds: which session is open, the in-flight answer stream
 * id, the per-turn model override, and the citation viewer target.
 */
import { create } from 'zustand';

/**
 * What the citation viewer is currently showing (AC-2 click-through). It carries
 * only what the chat/citation wire actually provides about the source (its id,
 * name, cited span, and optional player-relative media time/speaker). It
 * deliberately holds no freshness/last-indexed label: neither a media offset nor
 * the answer/message time says when the source was indexed, so the viewer never
 * presents either as indexing provenance (#120 GUARD).
 */
export interface DocumentViewerTarget {
  kind: 'document';
  id: string;
  handle?: string | null;
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
}

/** Public web source target; deliberately carries no corpus document identity. */
export interface WebViewerTarget {
  kind: 'web';
  id: string;
  handle: string;
  url: string;
  webTitle: string;
  snippet: string;
}

export type ViewerTarget = DocumentViewerTarget | WebViewerTarget;

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
   * The open side panel (spec 0007 #432): the conversation's context summary
   * or its artifacts viewer. Mutually exclusive with the citation viewer (one
   * aside slot); null when closed.
   */
  panel: 'context' | 'artifacts' | null;

  /**
   * Open a session. Its knowledge scope is NOT client state (#581): the
   * session query hydrates `effective_scope` from the server's pinned
   * assistant version, so nothing is seeded here from an assistant's head.
   */
  openSession: (sessionId: string) => void;
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
  panel: null,

  // Opening a session ends any in-flight stream and clears the viewer.
  openSession: (sessionId) =>
    set({
      activeSessionId: sessionId,
      activeStreamId: null,
      viewer: null,
      panel: null,
    }),
  closeSession: () =>
    set({
      activeSessionId: null,
      activeStreamId: null,
      viewer: null,
      panel: null,
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
