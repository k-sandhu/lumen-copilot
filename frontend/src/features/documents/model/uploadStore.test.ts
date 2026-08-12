/**
 * Upload store — client-side, ephemeral per-file upload state (#49 AC-2/AC-4).
 * This is genuine UI state (in-flight progress + transient errors), so it lives
 * in Zustand, NOT TanStack Query (frontend/AGENTS.md). The persisted Document
 * (status pending→…→ready) is server state and comes from useDocuments.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useUploadStore } from './uploadStore';

function reset() {
  useUploadStore.setState({ uploads: {} });
}

beforeEach(reset);

describe('uploadStore', () => {
  it('adds an upload in the bounded queue at 0 progress', () => {
    const id = useUploadStore.getState().add('msa.pdf', 'col-1');
    const u = useUploadStore.getState().uploads[id];
    expect(u?.filename).toBe('msa.pdf');
    expect(u?.collectionId).toBe('col-1');
    expect(u?.state).toBe('queued');
    expect(u?.progress).toBe(0);
  });

  it('tracks progress', () => {
    const id = useUploadStore.getState().add('a.pdf', 'col-1');
    useUploadStore.getState().setProgress(id, 0.42);
    expect(useUploadStore.getState().uploads[id]?.progress).toBe(0.42);
  });

  it('marks success and records the resulting document id', () => {
    const id = useUploadStore.getState().add('a.pdf', 'col-1');
    useUploadStore.getState().markSuccess(id, 'doc-99');
    const u = useUploadStore.getState().uploads[id];
    expect(u?.state).toBe('done');
    expect(u?.documentId).toBe('doc-99');
    expect(u?.progress).toBe(1);
  });

  it('marks an error with a message (413/415/etc. surfaced inline — AC-4)', () => {
    const id = useUploadStore.getState().add('huge.pdf', 'col-1');
    useUploadStore.getState().markError(id, 'File is larger than the 25 MB limit.');
    const u = useUploadStore.getState().uploads[id];
    expect(u?.state).toBe('error');
    expect(u?.error).toMatch(/25 MB/);
  });

  it('clears a stale terminal session id when the error is not resumable', () => {
    const id = useUploadStore.getState().add('meeting.mp3', 'col-1');
    useUploadStore.getState().setSession(id, 'expired-upload');
    useUploadStore.getState().markError(id, 'Upload expired.');

    expect(useUploadStore.getState().uploads[id]?.uploadId).toBeUndefined();
  });

  it('removes / dismisses an entry', () => {
    const id = useUploadStore.getState().add('a.pdf', 'col-1');
    useUploadStore.getState().remove(id);
    expect(useUploadStore.getState().uploads[id]).toBeUndefined();
  });

  it('clears all finished (done + error) entries but keeps in-flight ones', () => {
    const a = useUploadStore.getState().add('a.pdf', 'col-1');
    const b = useUploadStore.getState().add('b.pdf', 'col-1');
    const c = useUploadStore.getState().add('c.pdf', 'col-1');
    useUploadStore.getState().markSuccess(a, 'doc-a');
    useUploadStore.getState().markError(b, 'boom');
    // c stays uploading
    useUploadStore.getState().clearFinished();
    const { uploads } = useUploadStore.getState();
    expect(uploads[a]).toBeUndefined();
    expect(uploads[b]).toBeUndefined();
    expect(uploads[c]?.state).toBe('queued');
  });

  it('lists uploads for a given collection only', () => {
    useUploadStore.getState().add('a.pdf', 'col-1');
    useUploadStore.getState().add('b.pdf', 'col-2');
    const forCol1 = useUploadStore.getState().forCollection('col-1');
    expect(forCol1).toHaveLength(1);
    expect(forCol1[0]?.filename).toBe('a.pdf');
  });
});
