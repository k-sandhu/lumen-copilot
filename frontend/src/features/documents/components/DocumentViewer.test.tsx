/**
 * DocumentViewer (#49 AC-3, re-skinned #89; preview unified in #242/#245): a
 * drawer that surfaces the ingestion trace (parse → chunk → embed → ready) and
 * an optional cited passage. The document region is the shared
 * `DocumentPreviewBody` (its own unit test covers the fetch branches); here we
 * assert the drawer chrome (trace, cited passage, focus trap, close) and that a
 * ready document mounts the preview body while a non-ready one shows the
 * ingestion-pending message and fetches nothing.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';
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
  it('mounts the shared preview body for a ready document (blob iframe)', async () => {
    // The body fetches the bytes through the api/ boundary; a 200 blob → iframe.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Blob([new Uint8Array([1, 2, 3])], { type: 'application/pdf' }), {
        status: 200,
      }),
    );
    renderWithQuery(<DocumentViewer doc={doc} onClose={() => {}} />);

    const frame = await screen.findByTitle(/preview of msa.pdf/i);
    expect(frame).toHaveAttribute('sandbox', '');
    expect(frame.getAttribute('src')).toMatch(/^blob:/);
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

  it('traps Tab within the drawer — focus never escapes the open dialog (#163)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
    const user = userEvent.setup();
    renderWithQuery(<DocumentViewer doc={doc} onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');

    for (let i = 0; i < 5; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it('renders the parse → chunk → embed → ready ingestion trace (#89)', () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
    renderWithQuery(<DocumentViewer doc={{ ...doc, chunk_count: 142 }} onClose={() => {}} />);

    const ingestion = screen.getByRole('region', { name: /ingestion/i });
    expect(within(ingestion).getByText(/parsed/i)).toBeInTheDocument();
    expect(within(ingestion).getByText('Chunked into 142 passages')).toBeInTheDocument();
    expect(within(ingestion).getByText(/embedded/i)).toBeInTheDocument();
    expect(within(ingestion).getByText(/indexed & permission-scoped/i)).toBeInTheDocument();
  });

  it('surfaces a cited passage when one is supplied (#89 viewer drawer)', () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
    renderWithQuery(
      <DocumentViewer
        doc={doc}
        citedPassage={{ runs: [{ text: 'terminate for convenience', highlight: true }] }}
        onClose={() => {}}
      />,
    );
    const cited = screen.getByRole('region', { name: /cited passage/i });
    expect(within(cited).getByText('terminate for convenience').tagName).toBe('MARK');
  });

  it('explains a non-ready document has no preview yet (and skips the content fetch)', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    renderWithQuery(
      <DocumentViewer doc={{ ...doc, status: 'processing', chunk_count: 0 }} onClose={() => {}} />,
    );
    expect(screen.getByText(/preview is available once ingestion completes/i)).toBeInTheDocument();
    // No bytes are requested for a doc that isn't ready.
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
