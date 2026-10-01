/** Direct multipart upload UI: states, progress, cancellation and retry. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import {
  ApiError,
  DirectUploadError,
  documentUploadManager,
  type Document,
  type DocumentUploadSession,
} from '@/api';
import { DocumentUpload } from './DocumentUpload';
import { useUploadStore } from '../model/uploadStore';

const sampleDoc: Document = {
  id: 'doc-1',
  filename: 'msa.pdf',
  mime_type: 'application/pdf',
  size_bytes: 10,
  collection_id: 'col-1',
  owner_id: 'u-1',
  kind: 'document',
  duration_ms: null,
  status: 'pending',
  chunk_count: 0,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
};

function file(name = 'msa.pdf', type = 'application/pdf'): File {
  return new File([new Uint8Array(10)], name, { type });
}

beforeEach(() => {
  useUploadStore.setState({ uploads: {} });
});
afterEach(() => vi.restoreAllMocks());

describe('DocumentUpload', () => {
  it('is disabled with guidance when no collection is selected', () => {
    renderWithQuery(<DocumentUpload collectionId={undefined} />);
    expect(screen.getByText(/select a collection to upload/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /choose files/i })).not.toBeInTheDocument();
  });

  it('accepts browser-playable audio/video and shows aggregate multipart progress', async () => {
    let finish: ((document: Document) => void) | undefined;
    vi.spyOn(documentUploadManager, 'upload').mockImplementation(async (input) => {
      input.onProgress?.({
        phase: 'uploading',
        loadedBytes: 3,
        totalBytes: 10,
        fraction: 0.3,
      });
      return new Promise<Document>((resolve) => {
        finish = resolve;
      });
    });
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    const picker = screen.getByLabelText(/choose files to upload/i);
    expect(picker).toHaveAttribute('accept', expect.stringContaining('audio/mpeg'));
    expect(picker).toHaveAttribute('accept', expect.stringContaining('video/mp4'));

    await user.upload(picker, file());
    expect(await screen.findByRole('progressbar')).toHaveAttribute('aria-valuenow', '30');
    await act(async () => finish?.(sampleDoc));
    expect(await screen.findByText(/uploaded/i)).toBeInTheDocument();
  });

  it('shows the typed 413 detail inline', async () => {
    vi.spyOn(documentUploadManager, 'upload').mockRejectedValue(
      new ApiError('too large', 413, {
        type: 'about:blank',
        title: 'Payload Too Large',
        status: 413,
        detail: 'Files must be under 5 GB.',
      }),
    );
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('huge.mp4', 'video/mp4'),
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(/under 5 GB/i);
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('shows a clear unsupported-type error', async () => {
    vi.spyOn(documentUploadManager, 'upload').mockRejectedValue(new ApiError('unsupported', 415));
    const user = userEvent.setup({ applyAccept: false });
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('app.exe', 'application/x-msdownload'),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(/isn’t supported/i);
  });

  it('hands multiple files to the shared manager and renders each queue entry', async () => {
    vi.spyOn(documentUploadManager, 'upload').mockImplementation(
      () => new Promise(() => undefined),
    );
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    await user.upload(screen.getByLabelText(/choose files to upload/i), [
      file('a.pdf'),
      file('b.pdf'),
    ]);

    await waitFor(() => expect(documentUploadManager.upload).toHaveBeenCalledTimes(2));
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('b.pdf')).toBeInTheDocument();
  });

  it('cancels the active request and renders a terminal cancelled state', async () => {
    vi.spyOn(documentUploadManager, 'upload')
      .mockImplementationOnce(
        (input) =>
          new Promise<Document>((_resolve, reject) => {
            input.signal?.addEventListener('abort', () => {
              reject(
                new DirectUploadError('cancelled', new DOMException('aborted', 'AbortError'), {
                  cancelled: true,
                }),
              );
            });
          }),
      )
      .mockResolvedValueOnce(sampleDoc);
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('meeting.mp3', 'audio/mpeg'),
    );
    await user.click(await screen.findByRole('button', { name: /cancel meeting.mp3/i }));

    expect(await screen.findByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByText(/upload cancelled/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /start again/i }));
    await waitFor(() => expect(documentUploadManager.upload).toHaveBeenCalledTimes(2));
    expect(
      vi.mocked(documentUploadManager.upload).mock.calls[1]?.[0].resumeUploadId,
    ).toBeUndefined();
    expect(await screen.findByText(/uploaded/i)).toBeInTheDocument();
  });

  it('does not offer cancellation after the upload enters finalization', async () => {
    let finish: ((document: Document) => void) | undefined;
    vi.spyOn(documentUploadManager, 'upload').mockImplementation(async (input) => {
      input.onProgress?.({
        phase: 'completing',
        loadedBytes: 10,
        totalBytes: 10,
        fraction: 1,
      });
      return new Promise<Document>((resolve) => {
        finish = resolve;
      });
    });
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);

    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('meeting.mp3', 'audio/mpeg'),
    );

    expect(await screen.findByText(/finalizing/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel meeting.mp3/i })).not.toBeInTheDocument();
    await act(async () => finish?.(sampleDoc));
    expect(await screen.findByText(/uploaded/i)).toBeInTheDocument();
  });

  it('starts a new session when retrying an expired terminal upload', async () => {
    const terminalSession: DocumentUploadSession = {
      id: 'expired-upload',
      document_id: 'reserved-doc',
      state: 'expired',
      filename: 'meeting.mp3',
      mime_type: 'audio/mpeg',
      size_bytes: 10,
      collection_id: 'col-1',
      part_size_bytes: 5_242_880,
      part_count: 1,
      completed_parts: [],
      expires_at: '2026-08-11T00:00:00Z',
      error: 'Upload expired.',
      document: null,
      created_at: '2026-08-10T00:00:00Z',
      updated_at: '2026-08-11T00:00:00Z',
    };
    vi.spyOn(documentUploadManager, 'upload')
      .mockImplementationOnce(async (input) => {
        input.onSession?.(terminalSession);
        throw new DirectUploadError('Upload expired.', new ApiError('Upload expired.', 409));
      })
      .mockResolvedValueOnce(sampleDoc);
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);
    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('meeting.mp3', 'audio/mpeg'),
    );

    await user.click(await screen.findByRole('button', { name: /try again/i }));
    await waitFor(() => expect(documentUploadManager.upload).toHaveBeenCalledTimes(2));
    expect(
      vi.mocked(documentUploadManager.upload).mock.calls[1]?.[0].resumeUploadId,
    ).toBeUndefined();
    expect(await screen.findByText(/uploaded/i)).toBeInTheDocument();
  });
});
