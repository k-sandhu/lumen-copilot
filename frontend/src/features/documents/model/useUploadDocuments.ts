/**
 * Upload orchestration hook (#49 AC-2/AC-4). Bridges the `uploadDocument` api/
 * call to the ephemeral upload store: registers an entry per file, streams
 * progress into it, and on completion either marks success (and refreshes the
 * server document list so the new pending→…→ready row appears) or marks a
 * clear inline error for 413 / 415 / 422 / 404 / network (INV-8 / AC-4).
 *
 * Files are uploaded concurrently; each is independent so one failure never
 * stalls the rest.
 */
import { useCallback } from 'react';
import { ApiError, uploadDocument } from '@/api';
import { useUploadStore } from './uploadStore';
import { useRefreshDocuments } from './queries';

/** Map a failed upload to a clear, user-facing message (no leaking internals). */
export function uploadErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 413:
        return error.problem?.detail ?? 'File is too large to upload.';
      case 415:
        return error.problem?.detail ?? 'This file type isn’t supported.';
      case 422:
        return error.problem?.detail ?? 'The file couldn’t be accepted.';
      case 404:
        return 'That collection no longer exists.';
      case 401:
        return 'Your session expired — sign in again to upload.';
      case 0:
        return 'Upload failed — check your connection and try again.';
      default:
        return error.displayMessage || 'Upload failed.';
    }
  }
  return 'Upload failed.';
}

export function useUploadDocuments(collectionId: string | undefined) {
  const add = useUploadStore((s) => s.add);
  const setProgress = useUploadStore((s) => s.setProgress);
  const markSuccess = useUploadStore((s) => s.markSuccess);
  const markError = useUploadStore((s) => s.markError);
  const refreshDocuments = useRefreshDocuments();

  return useCallback(
    (files: File[]) => {
      if (!collectionId) return;
      for (const file of files) {
        const id = add(file.name, collectionId);
        uploadDocument({
          file,
          collectionId,
          onProgress: (fraction) => setProgress(id, fraction),
        })
          .then((doc) => {
            markSuccess(id, doc.id);
            refreshDocuments();
          })
          .catch((error: unknown) => {
            markError(id, uploadErrorMessage(error));
          });
      }
    },
    [collectionId, add, setProgress, markSuccess, markError, refreshDocuments],
  );
}
