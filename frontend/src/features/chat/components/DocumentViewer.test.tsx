/**
 * DocumentViewer (AC-2 click-through, INV-4): the cited document is loaded by an
 * AUTHENTICATED JSON capability request — the API boundary attaches the bearer,
 * then the viewer points at the short-lived storage URL. FastAPI never carries
 * the document bytes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { setAccessToken, clearAccessToken } from '@/api';
import { DocumentViewer } from './DocumentViewer';
import type { UiCitation } from '../model/citation';

const CITATION: UiCitation = {
  id: 'c1',
  documentId: 'doc-9',
  documentName: 'Q4 strategy.pdf',
  chunkId: 'k1',
  snippet: 'Revenue grew 12% in Q4.',
  charStart: 100,
  charEnd: 140,
};

const ACCESS_PATH = '/api/v2/documents/doc-9/access-url';

function accessResponse(): Response {
  return new Response(
    JSON.stringify({
      url: 'https://storage.example/doc-9?signed',
      filename: 'Q4 strategy.pdf',
      mime_type: 'application/pdf',
      size_bytes: 3,
      expires_at: '2030-01-01T00:00:00Z',
      purpose: 'preview',
      supports_byte_ranges: true,
    }),
    {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    },
  );
}

function problemResponse(status: number, title: string): Response {
  return new Response(JSON.stringify({ title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => {
  setAccessToken('jwt-abc');
});
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('DocumentViewer (authenticated content load — INV-4)', () => {
  it('loads the cited document through an authenticated request (bearer attached)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(accessResponse());

    render(<DocumentViewer citation={CITATION} onClose={() => {}} />);

    // The cited passage is shown immediately while the bytes load.
    expect(screen.getByText('Revenue grew 12% in Q4.')).toBeInTheDocument();
    expect(screen.getByText(/loading document/i)).toBeInTheDocument();

    await waitFor(() => {
      const iframe = screen.getByTitle('Preview of Q4 strategy.pdf');
      expect(iframe).toBeInTheDocument();
    });

    // The capability request carried the bearer; storage receives no bearer.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const call = fetchSpy.mock.calls[0];
    if (!call) throw new Error('fetch was not called');
    const [url, init] = call;
    expect(String(url)).toContain(ACCESS_PATH);
    const headers = new Headers(init?.headers);
    expect(headers.get('Authorization')).toBe('Bearer jwt-abc');

    // The iframe renders the signed storage URL, never the API endpoint.
    const iframe = screen.getByTitle('Preview of Q4 strategy.pdf') as HTMLIFrameElement;
    expect(iframe.getAttribute('src')).toBe('https://storage.example/doc-9?signed');
    expect(iframe.getAttribute('src')).not.toContain(ACCESS_PATH);
    expect(screen.getByRole('button', { name: /download original/i })).toBeInTheDocument();
  });

  it('renders "no longer available" with no retry on 404 (not-permitted / cross-tenant, INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problemResponse(404, 'Not Found'));

    render(<DocumentViewer citation={CITATION} onClose={() => {}} />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no longer available/i);
    // No iframe pointed at anything when the load fails.
    expect(screen.queryByTitle('Preview of Q4 strategy.pdf')).not.toBeInTheDocument();
    // INV-2: the UI never suggests access might appear — no retry on a 404.
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('does not fetch document bytes after receiving the access capability', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(accessResponse());
    render(<DocumentViewer citation={CITATION} onClose={() => {}} />);
    await screen.findByTitle('Preview of Q4 strategy.pdf');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  describe('source metadata grid (#120)', () => {
    beforeEach(() => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(accessResponse());
    });

    it('renders owner / last-modified / last-indexed rows', () => {
      render(<DocumentViewer citation={CITATION} onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('Owner')).toBeInTheDocument();
      expect(within(grid).getByText('Last modified')).toBeInTheDocument();
      expect(within(grid).getByText('Last indexed')).toBeInTheDocument();
    });

    it('shows ALL rows as "Not available" — the chat wire carries no source metadata (GUARD #120)', () => {
      // Owner, last-modified, AND last-indexed are all absent from the chat/
      // citation wire, so the grid shows honest placeholders for every one of them.
      render(<DocumentViewer citation={CITATION} onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getAllByText(/not available/i)).toHaveLength(3);
    });

    it('does NOT present a message/answer timestamp as "Last indexed" (GUARD #120 — no fabricated provenance)', () => {
      // The viewer is given no source-indexing timestamp (none exists on the chat
      // wire). Even though the answer was just produced, "Last indexed" must read
      // "Not available" — a doc indexed months ago must never show "Just now".
      render(<DocumentViewer citation={CITATION} onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      // The "Last indexed" label maps to a "Not available" value, not a recency.
      const labels = within(grid)
        .getAllByRole('term')
        .map((el) => el.textContent);
      const values = within(grid)
        .getAllByRole('definition')
        .map((el) => el.textContent);
      const indexedIdx = labels.indexOf('Last indexed');
      expect(indexedIdx).toBeGreaterThanOrEqual(0);
      expect(values[indexedIdx]).toMatch(/not available/i);
      // No relative-time recency leaked into the grid as source provenance.
      expect(within(grid).queryByText(/ago$/i)).not.toBeInTheDocument();
      expect(within(grid).queryByText(/^just now$/i)).not.toBeInTheDocument();
    });

    it('lights up the last-indexed row only when a REAL source-indexing value is supplied', () => {
      // A genuine source-indexing label (not the answer time) lights the row up.
      render(
        <DocumentViewer citation={CITATION} lastIndexed="Indexed 2d ago" onClose={() => {}} />,
      );
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('Indexed 2d ago')).toBeInTheDocument();
      // Owner + last-modified remain honest placeholders.
      expect(within(grid).getAllByText(/not available/i)).toHaveLength(2);
    });

    it('lights up the owner row when a source actually carries one', () => {
      render(<DocumentViewer citation={CITATION} owner="Priya Shah" onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('Priya Shah')).toBeInTheDocument();
    });
  });
});
