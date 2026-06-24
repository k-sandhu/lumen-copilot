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
 * name, and the cited span). It deliberately holds NO freshness/last-indexed
 * label: the only timestamp a chat turn has is the answer/message time, which is
 * the answer's age — not when the source was indexed — so the viewer never
 * presents it as source provenance (#120 GUARD against fabricated provenance).
 */
export interface ViewerTarget {
  documentId: string;
  documentName: string;
  charStart: number;
  charEnd: number;
  snippet: string;
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

  openSession: (sessionId: string) => void;
  closeSession: () => void;
  startStream: (streamId: string) => void;
  endStream: () => void;
  setPendingModel: (model: string | null) => void;
  openViewer: (target: ViewerTarget) => void;
  closeViewer: () => void;
}

export const useChatStore = create<ChatUiState>((set) => ({
  activeSessionId: null,
  activeStreamId: null,
  pendingModel: null,
  viewer: null,

  // Opening a session ends any in-flight stream and clears the viewer.
  openSession: (sessionId) =>
    set({ activeSessionId: sessionId, activeStreamId: null, viewer: null }),
  closeSession: () => set({ activeSessionId: null, activeStreamId: null, viewer: null }),
  startStream: (streamId) => set({ activeStreamId: streamId }),
  endStream: () => set({ activeStreamId: null }),
  setPendingModel: (model) => set({ pendingModel: model }),
  openViewer: (target) => set({ viewer: target }),
  closeViewer: () => set({ viewer: null }),
}));
