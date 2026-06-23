/**
 * Chat UI state (Zustand) — ONLY genuine client-side state (frontend/AGENTS.md).
 * Server data (sessions, messages, models) lives in TanStack Query and is NEVER
 * mirrored here. This holds: which session is open, the in-flight answer stream
 * id, the per-turn model override, and the citation viewer target.
 */
import { create } from 'zustand';

/** What the citation viewer is currently showing (AC-2 click-through). */
export interface ViewerTarget {
  documentId: string;
  documentName: string;
  charStart: number;
  charEnd: number;
  snippet: string;
  /** Source freshness label for the inspector (#89), e.g. "2d ago". */
  freshness?: string;
  /** Whether the source is past its freshness window. */
  stale?: boolean;
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
