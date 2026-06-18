/**
 * Upload store (Zustand) — ephemeral, client-side per-file upload tracking for
 * the documents slice (#49 AC-2/AC-4). This is genuine UI state: a transient
 * progress bar + a transient inline error per file the user just dropped. The
 * durable record is the `Document` (status pending→processing→ready→failed),
 * which is server state fetched by `useDocuments` — we do NOT mirror it here.
 *
 * Lifecycle: add (uploading@0) → setProgress* → markSuccess(documentId) | markError(msg).
 * Finished entries (done|error) are cleared once the server list shows the
 * resulting document, or dismissed by the user.
 */
import { create } from 'zustand';

export type UploadState = 'uploading' | 'done' | 'error';

export interface UploadEntry {
  id: string;
  filename: string;
  collectionId: string;
  state: UploadState;
  /** 0..1 while uploading; 1 once done. */
  progress: number;
  /** Inline error message (e.g. 413/415 surfaced from the Problem body). */
  error?: string;
  /** The created Document id, once the upload succeeds. */
  documentId?: string;
}

interface UploadStoreState {
  uploads: Record<string, UploadEntry>;
  /** Register a new upload; returns its client-side id. */
  add: (filename: string, collectionId: string) => string;
  setProgress: (id: string, progress: number) => void;
  markSuccess: (id: string, documentId: string) => void;
  markError: (id: string, message: string) => void;
  remove: (id: string) => void;
  /** Drop all done|error entries; keep anything still uploading. */
  clearFinished: () => void;
  /** Entries for one collection, in insertion order. */
  forCollection: (collectionId: string) => UploadEntry[];
}

let counter = 0;
function nextId(): string {
  counter += 1;
  return `upl-${Date.now().toString(36)}-${counter}`;
}

export const useUploadStore = create<UploadStoreState>((set, get) => ({
  uploads: {},

  add: (filename, collectionId) => {
    const id = nextId();
    const entry: UploadEntry = { id, filename, collectionId, state: 'uploading', progress: 0 };
    set((s) => ({ uploads: { ...s.uploads, [id]: entry } }));
    return id;
  },

  setProgress: (id, progress) =>
    set((s) => {
      const existing = s.uploads[id];
      if (!existing) return s;
      return { uploads: { ...s.uploads, [id]: { ...existing, progress } } };
    }),

  markSuccess: (id, documentId) =>
    set((s) => {
      const existing = s.uploads[id];
      if (!existing) return s;
      return {
        uploads: {
          ...s.uploads,
          [id]: { ...existing, state: 'done', progress: 1, documentId },
        },
      };
    }),

  markError: (id, message) =>
    set((s) => {
      const existing = s.uploads[id];
      if (!existing) return s;
      return { uploads: { ...s.uploads, [id]: { ...existing, state: 'error', error: message } } };
    }),

  remove: (id) =>
    set((s) => {
      const next = { ...s.uploads };
      delete next[id];
      return { uploads: next };
    }),

  clearFinished: () =>
    set((s) => {
      const next: Record<string, UploadEntry> = {};
      for (const [id, entry] of Object.entries(s.uploads)) {
        if (entry.state === 'uploading') next[id] = entry;
      }
      return { uploads: next };
    }),

  forCollection: (collectionId) =>
    Object.values(get().uploads).filter((u) => u.collectionId === collectionId),
}));
