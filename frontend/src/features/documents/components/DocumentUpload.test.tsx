/**
 * DocumentUpload (#49 AC-2/AC-4): disabled until a collection is selected;
 * picking files uploads via XHR multipart with live progress; a successful
 * upload shows "Uploaded"; a 413 (too large) and a 415 (unsupported type)
 * surface as clear INLINE errors per file (AC-4).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { DocumentUpload } from './DocumentUpload';
import { useUploadStore } from '../model/uploadStore';

interface XhrStub {
  open: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  setRequestHeader: ReturnType<typeof vi.fn>;
  upload: { onprogress: ((e: ProgressEvent) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
  withCredentials: boolean;
}

function installXhr(): { all: XhrStub[] } {
  const all: XhrStub[] = [];
  class FakeXhr {
    open = vi.fn();
    setRequestHeader = vi.fn();
    upload: XhrStub['upload'] = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    status = 0;
    responseText = '';
    withCredentials = false;
    send = vi.fn();
    constructor() {
      all.push(this as unknown as XhrStub);
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXhr as unknown as typeof XMLHttpRequest);
  return { all };
}

const sampleDoc = {
  id: 'doc-1',
  filename: 'msa.pdf',
  mime_type: 'application/pdf',
  size_bytes: 10,
  collection_id: 'col-1',
  owner_id: 'u-1',
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
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('DocumentUpload', () => {
  it('is disabled with guidance when no collection is selected', () => {
    renderWithQuery(<DocumentUpload collectionId={undefined} />);
    expect(screen.getByText(/select a collection to upload/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /choose files/i })).not.toBeInTheDocument();
  });

  it('uploads a picked file with progress, then shows Uploaded (AC-2)', async () => {
    const ref = installXhr();
    // The fetch for the post-upload refresh (invalidateQueries) — stub it.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);

    await user.upload(screen.getByLabelText(/choose files to upload/i), file());

    // Progress bar visible mid-upload.
    const xhr = ref.all[0];
    expect(xhr).toBeTruthy();
    act(() =>
      xhr?.upload.onprogress?.({ lengthComputable: true, loaded: 30, total: 100 } as ProgressEvent),
    );
    expect(await screen.findByRole('progressbar')).toHaveAttribute('aria-valuenow', '30');

    // Complete the upload (201).
    act(() => {
      if (xhr) {
        xhr.status = 201;
        xhr.responseText = JSON.stringify(sampleDoc);
        xhr.onload?.();
      }
    });
    expect(await screen.findByText(/uploaded/i)).toBeInTheDocument();
  });

  it('shows a clear inline error when the file is too large (413 — AC-4)', async () => {
    const ref = installXhr();
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);

    await user.upload(screen.getByLabelText(/choose files to upload/i), file('huge.pdf'));
    const xhr = ref.all[0];
    act(() => {
      if (xhr) {
        xhr.status = 413;
        xhr.responseText = JSON.stringify({
          type: 'about:blank',
          title: 'Payload Too Large',
          status: 413,
          detail: 'Files must be under 25 MB.',
        });
        xhr.onload?.();
      }
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/under 25 MB/i);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
  });

  it('shows a clear inline error for an unsupported type (415 — AC-4)', async () => {
    const ref = installXhr();
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);

    await user.upload(
      screen.getByLabelText(/choose files to upload/i),
      file('app.exe', 'application/x-msdownload'),
    );
    const xhr = ref.all[0];
    act(() => {
      if (xhr) {
        xhr.status = 415;
        xhr.responseText = JSON.stringify({
          type: 'about:blank',
          title: 'Unsupported Media Type',
          status: 415,
        });
        xhr.onload?.();
      }
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/isn’t supported/i);
  });

  it('uploads multiple files concurrently (one XHR each)', async () => {
    const ref = installXhr();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const user = userEvent.setup();
    renderWithQuery(<DocumentUpload collectionId="col-1" />);

    await user.upload(screen.getByLabelText(/choose files to upload/i), [
      file('a.pdf'),
      file('b.pdf'),
    ]);
    await waitFor(() => expect(ref.all.length).toBe(2));
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('b.pdf')).toBeInTheDocument();
  });
});
