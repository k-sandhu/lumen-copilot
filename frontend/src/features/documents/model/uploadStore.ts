/** Ephemeral per-file UI state for the resumable multipart uploader (#571). */
import { create } from 'zustand';
import type { DirectUploadProgress } from '@/api';

export type UploadState =
  | 'queued'
  | 'initiating'
  | 'resuming'
  | 'uploading'
  | 'completing'
  | 'done'
  | 'cancelled'
  | 'error';

export interface UploadEntry {
  id: string;
  filename: string;
  collectionId: string;
  state: UploadState;
  progress: number;
  loadedBytes: number;
  totalBytes: number;
  /** Kept only in memory so the user can retry/resume in this browser session. */
  file?: File;
  error?: string;
  documentId?: string;
  uploadId?: string;
}

interface UploadStoreState {
  uploads: Record<string, UploadEntry>;
  add: (file: File | string, collectionId: string) => string;
  setProgress: (id: string, progress: DirectUploadProgress | number) => void;
  setSession: (id: string, uploadId: string) => void;
  markSuccess: (id: string, documentId: string) => void;
  markError: (id: string, message: string, uploadId?: string) => void;
  markCancelled: (id: string) => void;
  prepareRetry: (id: string) => void;
  remove: (id: string) => void;
  clearFinished: () => void;
  forCollection: (collectionId: string) => UploadEntry[];
}

let counter = 0;
function nextId(): string {
  counter += 1;
  return `upl-${Date.now().toString(36)}-${counter}`;
}

const ACTIVE_STATES = new Set<UploadState>([
  'queued',
  'initiating',
  'resuming',
  'uploading',
  'completing',
]);

export function isActiveUpload(state: UploadState): boolean {
  return ACTIVE_STATES.has(state);
}

const CANCELLABLE_STATES = new Set<UploadState>(['queued', 'initiating', 'resuming', 'uploading']);

/** Finalization is an idempotent commit boundary and cannot be safely cancelled. */
export function isCancellableUpload(state: UploadState): boolean {
  return CANCELLABLE_STATES.has(state);
}

export const useUploadStore = create<UploadStoreState>((set, get) => ({
  uploads: {},

  add: (fileOrName, collectionId) => {
    const id = nextId();
    const file = typeof fileOrName === 'string' ? undefined : fileOrName;
    const entry: UploadEntry = {
      id,
      filename: typeof fileOrName === 'string' ? fileOrName : fileOrName.name,
      collectionId,
      state: 'queued',
      progress: 0,
      loadedBytes: 0,
      totalBytes: file?.size ?? 0,
      ...(file ? { file } : {}),
    };
    set((state) => ({ uploads: { ...state.uploads, [id]: entry } }));
    return id;
  },

  setProgress: (id, progress) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      const update =
        typeof progress === 'number'
          ? { progress, loadedBytes: progress * existing.totalBytes }
          : {
              state: progress.phase,
              progress: progress.fraction,
              loadedBytes: progress.loadedBytes,
              totalBytes: progress.totalBytes,
            };
      return { uploads: { ...state.uploads, [id]: { ...existing, ...update } } };
    }),

  setSession: (id, uploadId) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      return { uploads: { ...state.uploads, [id]: { ...existing, uploadId } } };
    }),

  markSuccess: (id, documentId) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      return {
        uploads: {
          ...state.uploads,
          [id]: {
            ...existing,
            state: 'done',
            progress: 1,
            loadedBytes: existing.totalBytes,
            documentId,
            error: undefined,
          },
        },
      };
    }),

  markError: (id, message, uploadId) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      return {
        uploads: {
          ...state.uploads,
          [id]: {
            ...existing,
            state: 'error',
            error: message,
            uploadId,
          },
        },
      };
    }),

  markCancelled: (id) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      return {
        uploads: {
          ...state.uploads,
          [id]: { ...existing, state: 'cancelled', error: undefined, uploadId: undefined },
        },
      };
    }),

  prepareRetry: (id) =>
    set((state) => {
      const existing = state.uploads[id];
      if (!existing) return state;
      return {
        uploads: {
          ...state.uploads,
          [id]: { ...existing, state: existing.uploadId ? 'resuming' : 'queued', error: undefined },
        },
      };
    }),

  remove: (id) =>
    set((state) => {
      const uploads = { ...state.uploads };
      delete uploads[id];
      return { uploads };
    }),

  clearFinished: () =>
    set((state) => ({
      uploads: Object.fromEntries(
        Object.entries(state.uploads).filter(([, entry]) => isActiveUpload(entry.state)),
      ),
    })),

  forCollection: (collectionId) =>
    Object.values(get().uploads).filter((upload) => upload.collectionId === collectionId),
}));
