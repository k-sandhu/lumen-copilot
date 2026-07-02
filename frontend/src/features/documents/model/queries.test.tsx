/**
 * useDocuments (#272) — TanStack Query over the documents api/ boundary against a
 * mocked fetch. The headline is the keepPreviousData behaviour: when the filename
 * filter changes, the previous page's rows stay visible (as placeholder data)
 * while the refined query loads, so the table never flashes to skeletons on every
 * keystroke — matching the tested useSearch / useAuditEvents peers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { setAccessToken, clearAccessToken } from '@/api';
import type { Document, DocumentList } from '@/api';
import { useDocuments } from './queries';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function doc(id: string, filename: string): Document {
  return {
    id,
    filename,
    mime_type: 'text/plain',
    size_bytes: 1,
    collection_id: 'col-1',
    owner_id: 'u-1',
    status: 'ready',
    chunk_count: 1,
    created_at: '2026-06-18T00:00:00Z',
    updated_at: '2026-06-18T00:00:00Z',
  };
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('useDocuments', () => {
  it('keeps the previous rows visible while a refined filename filter loads (#272)', async () => {
    const first: DocumentList = { items: [doc('d1', 'alpha.txt')], next_cursor: null };
    // The refined query never resolves during the assertion window, so without
    // keepPreviousData the hook would be `pending` with no data (the table flash).
    let releaseSecond: (r: Response) => void = () => {};
    const secondPending = new Promise<Response>((resolve) => {
      releaseSecond = resolve;
    });
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json(first))
      .mockReturnValueOnce(secondPending);

    const { result, rerender } = renderHook(
      ({ q }: { q: string }) => useDocuments({ collectionId: 'col-1', q }),
      { wrapper: wrapper(), initialProps: { q: '' } },
    );

    await waitFor(() => expect(result.current.data?.items[0]?.filename).toBe('alpha.txt'));

    // Change the filter → a new (still-pending) query key. The previous rows must
    // remain as placeholder data rather than the hook flipping to pending/no-data.
    rerender({ q: 'al' });
    expect(result.current.data?.items[0]?.filename).toBe('alpha.txt');
    expect(result.current.isPlaceholderData).toBe(true);

    // Resolve the refined query → the rows update.
    releaseSecond(json({ items: [doc('d1', 'alpha.txt')], next_cursor: null }));
    await waitFor(() => expect(result.current.isPlaceholderData).toBe(false));
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('is disabled until a collection is selected (no "all documents" view)', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const { result } = renderHook(() => useDocuments({ collectionId: undefined }), {
      wrapper: wrapper(),
    });
    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
