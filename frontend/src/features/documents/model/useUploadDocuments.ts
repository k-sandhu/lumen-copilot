/** React bridge for the bounded direct multipart manager (spec 0008 / #571). */
import { useCallback } from 'react';
import { ApiError, DirectUploadError, documentUploadManager } from '@/api';
import { isCancellableUpload, useUploadStore } from './uploadStore';
import { useRefreshDocuments } from './queries';

const controllers = new Map<string, AbortController>();

/** Map a failed control/storage operation to a clear, non-provider-leaking message. */
export function uploadErrorMessage(error: unknown): string {
  const cause = error instanceof DirectUploadError ? error.causeValue : error;
  if (cause instanceof ApiError) {
    switch (cause.status) {
      case 413:
        return cause.problem?.detail ?? 'File is too large to upload.';
      case 415:
        return cause.problem?.detail ?? 'This file type isn’t supported.';
      case 422:
        return cause.problem?.detail ?? 'The file couldn’t be accepted.';
      case 404:
        return 'That collection or upload session is no longer available.';
      case 409:
        return cause.problem?.detail ?? 'This upload can’t continue in its current state.';
      case 401:
        return 'Your session expired — sign in again to upload.';
      case 0:
        return 'Upload failed — check your connection and try again.';
      default:
        return cause.displayMessage || 'Upload failed.';
    }
  }
  return 'Upload failed — check your connection and try again.';
}

export interface UploadDocumentsActions {
  start: (files: File[]) => void;
  cancel: (uploadEntryId: string) => void;
  retry: (uploadEntryId: string) => void;
}

export function useUploadDocuments(collectionId: string | undefined): UploadDocumentsActions {
  const add = useUploadStore((state) => state.add);
  const setProgress = useUploadStore((state) => state.setProgress);
  const setSession = useUploadStore((state) => state.setSession);
  const markSuccess = useUploadStore((state) => state.markSuccess);
  const markError = useUploadStore((state) => state.markError);
  const markCancelled = useUploadStore((state) => state.markCancelled);
  const prepareRetry = useUploadStore((state) => state.prepareRetry);
  const refreshDocuments = useRefreshDocuments();

  const run = useCallback(
    (entryId: string, file: File, targetCollectionId: string, resumeUploadId?: string) => {
      const controller = new AbortController();
      controllers.set(entryId, controller);
      void documentUploadManager
        .upload({
          file,
          collectionId: targetCollectionId,
          ...(resumeUploadId ? { resumeUploadId } : {}),
          signal: controller.signal,
          onProgress: (progress) => setProgress(entryId, progress),
          onSession: (session) => setSession(entryId, session.id),
        })
        .then((document) => {
          markSuccess(entryId, document.id);
          refreshDocuments();
        })
        .catch((error: unknown) => {
          if (error instanceof DirectUploadError && error.cancelled) {
            markCancelled(entryId);
            return;
          }
          markError(
            entryId,
            uploadErrorMessage(error),
            error instanceof DirectUploadError ? error.uploadId : undefined,
          );
        })
        .finally(() => {
          if (controllers.get(entryId) === controller) controllers.delete(entryId);
        });
    },
    [markCancelled, markError, markSuccess, refreshDocuments, setProgress, setSession],
  );

  const start = useCallback(
    (files: File[]) => {
      if (!collectionId) return;
      for (const file of files) {
        const entryId = add(file, collectionId);
        run(entryId, file, collectionId);
      }
    },
    [add, collectionId, run],
  );

  const cancel = useCallback((entryId: string) => {
    const entry = useUploadStore.getState().uploads[entryId];
    if (!entry || !isCancellableUpload(entry.state)) return;
    controllers.get(entryId)?.abort();
  }, []);

  const retry = useCallback(
    (entryId: string) => {
      const entry = useUploadStore.getState().uploads[entryId];
      if (!entry?.file || (entry.state !== 'error' && entry.state !== 'cancelled')) return;
      prepareRetry(entryId);
      run(entryId, entry.file, entry.collectionId, entry.uploadId);
    },
    [prepareRetry, run],
  );

  return { start, cancel, retry };
}
