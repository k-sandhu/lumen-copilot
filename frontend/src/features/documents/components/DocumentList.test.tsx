/**
 * DocumentList (#49 AC-3/AC-4) across states: loading, error (retry), empty (vs.
 * filtered-empty), success; a `failed` row shows its error inline (AC-4); status
 * + filename filters pass through to GET /documents; delete confirms and fires
 * DELETE /documents/{id}.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { DocumentList } from './DocumentList';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const doc = (over: Partial<Record<string, unknown>> = {}) => ({
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
  ...over,
});

afterEach(() => vi.restoreAllMocks());
beforeEach(() => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

describe('DocumentList', () => {
  it('renders a loading state then the documents', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    expect(screen.getByText(/loading documents/i)).toBeInTheDocument();
    expect(await screen.findByText('msa.pdf')).toBeInTheDocument();
    // "Ready" also appears as a filter chip — assert the status badge on the row.
    const list = screen.getByRole('list', { name: /documents/i });
    expect(within(list).getByText('Ready')).toBeInTheDocument();
  });

  it('shows the EMPTY state for a collection with no documents', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    expect(await screen.findByText(/this collection is empty/i)).toBeInTheDocument();
  });

  it('shows a failed document’s error inline (AC-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        items: [doc({ status: 'failed', error: 'Could not parse the PDF (corrupt).' })],
        next_cursor: null,
      }),
    );
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    expect(await screen.findByText(/could not parse the pdf/i)).toBeInTheDocument();
    // "Failed" is both a filter chip and the row badge — assert within the list.
    const list = screen.getByRole('list', { name: /documents/i });
    expect(within(list).getByText('Failed')).toBeInTheDocument();
  });

  it('disables Open until the document is ready', async () => {
    // `processing` is unsettled → the list polls; serve a fresh Response per poll.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      jsonResponse({ items: [doc({ status: 'processing' })], next_cursor: null }),
    );
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    await screen.findByText('msa.pdf');
    expect(screen.getByRole('button', { name: /open/i })).toBeDisabled();
  });

  it('opens a ready document', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={onOpen} />);
    await screen.findByText('msa.pdf');
    await user.click(screen.getByRole('button', { name: /open/i }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'doc-1' }));
  });

  it('passes the status filter and filename q through to the request (AC-3)', async () => {
    // Each list call reads the Response body once; return a FRESH Response per
    // call (a Response body can only be consumed once — see client.test.ts).
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async () => jsonResponse({ items: [], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);

    // Wait for the initial (unfiltered) empty render so the query has settled.
    await screen.findByText(/this collection is empty/i);

    await user.click(screen.getByRole('button', { name: 'Failed' }));
    await user.type(screen.getByLabelText(/filter documents by filename/i), 'msa');

    // The filtered-empty message differs from the empty-collection message.
    expect(await screen.findByText(/no documents match these filters/i)).toBeInTheDocument();

    const last = fetchSpy.mock.calls.at(-1)?.[0] as string;
    expect(last).toContain('collection_id=col-1');
    expect(last).toContain('status=failed');
    expect(last).toContain('q=msa');
  });

  it('deletes a document after confirm (AC-3)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ items: [doc()], next_cursor: null }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);

    await screen.findByText('msa.pdf');
    await user.click(screen.getByRole('button', { name: /delete msa.pdf/i }));
    await waitFor(() => {
      const del = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'DELETE');
      expect(del?.[0]).toContain('/documents/doc-1');
    });
  });

  it('shows an actionable error with retry on a list failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error'));
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/couldn’t load documents/i);
  });
});
