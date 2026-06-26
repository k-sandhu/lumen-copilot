/**
 * DocumentList (#49 AC-3/AC-4 · #119 table polish) across states: loading, error
 * (retry), empty (vs. filtered-empty), success; a `failed` row shows its error
 * inline (AC-4); status + filename filters pass through to GET /documents; delete
 * confirms and fires DELETE /documents/{id}. The list is now a TABLE whose
 * columns (Name + type badge, Collection, Visibility, Owner, Updated, Status) are
 * all backed by real document fields.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
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

describe('DocumentList', () => {
  it('renders a loading state then the documents table', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    expect(screen.getByText(/loading documents/i)).toBeInTheDocument();
    expect(await screen.findByText('msa.pdf')).toBeInTheDocument();
    // The status renders in the row's Status cell.
    const table = screen.getByRole('table');
    expect(within(table).getByText('Ready')).toBeInTheDocument();
    // Column headers reflect the wireframe columns.
    for (const col of ['Name', 'Collection', 'Visibility', 'Owner', 'Updated', 'Status']) {
      expect(within(table).getByRole('columnheader', { name: col })).toBeInTheDocument();
    }
  });

  it('renders the wireframe columns from real fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    renderWithQuery(
      <DocumentList
        collectionId="col-1"
        collectionName="Contracts"
        currentUserId="u-1"
        onOpen={() => {}}
      />,
    );
    await screen.findByText('msa.pdf');
    const table = screen.getByRole('table');
    // Collection column shows the selected collection's name.
    expect(within(table).getByText('Contracts')).toBeInTheDocument();
    // Owner column reads "You" for the current user's own document.
    expect(within(table).getByText('You')).toBeInTheDocument();
    // Visibility renders the kit PermissionPill (owner-only invariant, not a
    // fabricated visibility level) — "Private to you" for your own doc.
    expect(within(table).getByText('Private to you')).toBeInTheDocument();
    // File-type label (PDF) appears in the name cell — as the badge glyph and the
    // sub-line caption.
    expect(within(table).getAllByText('PDF').length).toBeGreaterThan(0);
    // Updated column renders a FreshnessPill (relative time) — aria-labelled with
    // the machine timestamp.
    expect(within(table).getByLabelText(/2026-06-18T00:00:00Z/)).toBeInTheDocument();
  });

  it('labels another user’s document as owner-only, not "You"', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc({ owner_id: 'someone-else-123' })], next_cursor: null }),
    );
    renderWithQuery(
      <DocumentList collectionId="col-1" currentUserId="u-1" onOpen={() => {}} />,
    );
    await screen.findByText('msa.pdf');
    const table = screen.getByRole('table');
    expect(within(table).queryByText('You')).not.toBeInTheDocument();
    expect(within(table).getByText('Owner only')).toBeInTheDocument();
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
    const table = screen.getByRole('table');
    expect(within(table).getByText('Failed')).toBeInTheDocument();
  });

  it('does not make a non-ready row openable', async () => {
    // `processing` is unsettled → the list polls; serve a fresh Response per poll.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      jsonResponse({ items: [doc({ status: 'processing' })], next_cursor: null }),
    );
    const onOpen = vi.fn();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={onOpen} />);
    await screen.findByText('msa.pdf');
    // A processing row is not a button (no preview yet) and never opens the viewer.
    const table = screen.getByRole('table');
    expect(within(table).queryByRole('button', { name: /open msa\.pdf/i })).not.toBeInTheDocument();
  });

  it('opens a ready document by clicking its row', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={onOpen} />);
    await screen.findByText('msa.pdf');
    await user.click(screen.getByRole('button', { name: /open msa\.pdf/i }));
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

  it('deletes a document after confirming in the dialog (AC-3)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ items: [doc()], next_cursor: null }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);

    await screen.findByText('msa.pdf');
    // Clicking Delete opens the focus-managed confirm dialog — it does NOT delete yet.
    await user.click(screen.getByRole('button', { name: /delete msa.pdf/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/delete document\?/i)).toBeInTheDocument();
    expect(
      fetchSpy.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE'),
    ).toBe(false);

    // Confirming fires DELETE /documents/doc-1.
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));
    await waitFor(() => {
      const del = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'DELETE');
      expect(del?.[0]).toContain('/documents/doc-1');
    });
  });

  it('does not delete when the confirm dialog is cancelled', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [doc()], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);

    await screen.findByText('msa.pdf');
    await user.click(screen.getByRole('button', { name: /delete msa.pdf/i }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(fetchSpy.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE')).toBe(false);
  });

  it('does not open the viewer when opening the delete dialog (action click is isolated)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={onOpen} />);
    await screen.findByText('msa.pdf');
    await user.click(screen.getByRole('button', { name: /delete msa.pdf/i }));
    await screen.findByRole('alertdialog');
    expect(onOpen).not.toHaveBeenCalled();
  });

  // Regression (review #125): the row is a `role="button"` whose onKeyDown opens
  // the viewer on Enter/Space. The Delete button only stopped CLICK propagation,
  // so a keyboard activation of Delete bubbled its keydown to the row and opened
  // the viewer alongside the delete. Keyboard activation of Delete must open the
  // confirm dialog and NOT open the viewer — for both Enter and Space.
  it.each([
    ['Enter', '{Enter}'],
    ['Space', ' '],
  ])('does not open the viewer when activating Delete with the %s key', async (_name, keys) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [doc()], next_cursor: null }),
    );
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={onOpen} />);
    await screen.findByText('msa.pdf');

    // Focus the Delete button directly (its tab order is after the openable row),
    // then activate it with the keyboard.
    const del = screen.getByRole('button', { name: /delete msa.pdf/i });
    del.focus();
    expect(del).toHaveFocus();
    await user.keyboard(keys);

    // The confirm dialog opened …
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
    // … and the viewer never opened (the keydown did not bubble to the row).
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('shows an actionable error with retry on a list failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error'));
    renderWithQuery(<DocumentList collectionId="col-1" onOpen={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/couldn’t load documents/i);
  });
});
