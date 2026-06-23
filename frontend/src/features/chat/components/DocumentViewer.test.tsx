/**
 * DocumentViewer (AC-2 click-through, INV-4): the cited document is loaded by an
 * AUTHENTICATED request — the api/ boundary attaches the bearer access token and
 * the viewer renders the bytes as a blob: URL. It must NOT point a bare
 * <iframe src>/<a href> at the bearer-authed `GET /documents/{id}/content`
 * endpoint (a browser iframe/anchor GET cannot send Authorization, so it would
 * 401). Covers: authenticated load → ready, loading + error (404) states, the
 * blob: object URL is revoked on unmount, and the rendered <iframe>/<a> never
 * target the raw content endpoint.
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

const CONTENT_PATH = '/documents/doc-9/content';

function bytesResponse(): Response {
  return new Response(new Blob([new Uint8Array([1, 2, 3])]), {
    status: 200,
    headers: { 'Content-Type': 'application/octet-stream' },
  });
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
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(bytesResponse());

    render(<DocumentViewer citation={CITATION} onClose={() => {}} />);

    // The cited passage is shown immediately while the bytes load.
    expect(screen.getByText('Revenue grew 12% in Q4.')).toBeInTheDocument();
    expect(screen.getByText(/loading document/i)).toBeInTheDocument();

    await waitFor(() => {
      const iframe = screen.getByTitle('Document: Q4 strategy.pdf');
      expect(iframe).toBeInTheDocument();
    });

    // The content request carried the bearer — NOT a bare unauthenticated GET.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const call = fetchSpy.mock.calls[0];
    if (!call) throw new Error('fetch was not called');
    const [url, init] = call;
    expect(String(url)).toContain(CONTENT_PATH);
    const headers = new Headers(init?.headers);
    expect(headers.get('Authorization')).toBe('Bearer jwt-abc');

    // The iframe/anchor render the blob: URL, never the bearer-authed endpoint.
    const iframe = screen.getByTitle('Document: Q4 strategy.pdf') as HTMLIFrameElement;
    expect(iframe.getAttribute('src')).toMatch(/^blob:/);
    expect(iframe.getAttribute('src')).not.toContain(CONTENT_PATH);
    const anchor = screen.getByRole('link', { name: /open original/i });
    expect(anchor.getAttribute('href')).toMatch(/^blob:/);
    expect(anchor.getAttribute('href')).not.toContain(CONTENT_PATH);
  });

  it('surfaces a 404 (not-permitted / cross-tenant, INV-2) as an actionable error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problemResponse(404, 'Not Found'),
    );

    render(<DocumentViewer citation={CITATION} onClose={() => {}} />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not open this document/i);
    // No iframe pointed at anything when the load fails.
    expect(screen.queryByTitle('Document: Q4 strategy.pdf')).not.toBeInTheDocument();
    // The error is recoverable, not a dead end.
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('revokes the blob: object URL on unmount (no leak)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(bytesResponse());
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL');

    const { unmount } = render(<DocumentViewer citation={CITATION} onClose={() => {}} />);
    await screen.findByTitle('Document: Q4 strategy.pdf');

    unmount();
    expect(revokeSpy).toHaveBeenCalled();
  });

  describe('source metadata grid (#120)', () => {
    beforeEach(() => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(bytesResponse());
    });

    it('renders owner / last-modified / last-indexed rows', () => {
      render(<DocumentViewer citation={CITATION} freshness="2d ago" onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('Owner')).toBeInTheDocument();
      expect(within(grid).getByText('Last modified')).toBeInTheDocument();
      expect(within(grid).getByText('Last indexed')).toBeInTheDocument();
    });

    it('shows the real derived freshness as last-indexed', () => {
      render(<DocumentViewer citation={CITATION} freshness="2d ago" onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('2d ago')).toBeInTheDocument();
    });

    it('shows "Not available" — never a fabricated name — for owner off the wire (GUARD #120)', () => {
      render(<DocumentViewer citation={CITATION} freshness="2d ago" onClose={() => {}} />);
      const grid = screen.getByRole('group', { name: /source metadata/i });
      // Owner + last-modified are not on the chat wire → honest placeholders.
      expect(within(grid).getAllByText(/not available/i).length).toBeGreaterThanOrEqual(2);
    });

    it('lights up the owner row when a source actually carries one', () => {
      render(
        <DocumentViewer citation={CITATION} owner="Priya Shah" freshness="2d ago" onClose={() => {}} />,
      );
      const grid = screen.getByRole('group', { name: /source metadata/i });
      expect(within(grid).getByText('Priya Shah')).toBeInTheDocument();
    });
  });
});
