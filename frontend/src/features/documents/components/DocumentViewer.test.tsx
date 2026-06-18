/**
 * DocumentViewer (#49 AC-3): resolves GET /documents/{id}/content, following a
 * 302 to a presigned URL, and renders it in a sandboxed iframe. States: loading,
 * success (iframe src = presigned URL), and the not-permitted 404 negative
 * (INV-2) → a clear "no longer available" message with no retry.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { DocumentViewer } from './DocumentViewer';
import type { Document } from '@/api';

const doc: Document = {
  id: 'doc-1',
  filename: 'msa.pdf',
  mime_type: 'application/pdf',
  size_bytes: 2048,
  collection_id: 'col-1',
  owner_id: 'u-1',
  status: 'ready',
  chunk_count: 5,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
};

afterEach(() => vi.restoreAllMocks());

describe('DocumentViewer', () => {
  it('follows a 302 and renders the presigned URL in a sandboxed iframe (AC-3)', async () => {
    const presigned = 'https://minio.example/bucket/obj?sig=abc';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 302, headers: { Location: presigned } }),
    );
    renderWithQuery(<DocumentViewer doc={doc} onClose={() => {}} />);

    const frame = await screen.findByTitle(/preview of msa.pdf/i);
    expect(frame).toHaveAttribute('src', presigned);
    expect(frame).toHaveAttribute('sandbox', '');
  });

  it('shows a clear message and no retry when the document is not permitted (404 — INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ type: 'about:blank', title: 'Not Found', status: 404 }), {
        status: 404,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    );
    renderWithQuery(<DocumentViewer doc={doc} onClose={() => {}} />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no longer available/i);
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('closes on the Close button and on Escape', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<DocumentViewer doc={doc} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: /close viewer/i }));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
