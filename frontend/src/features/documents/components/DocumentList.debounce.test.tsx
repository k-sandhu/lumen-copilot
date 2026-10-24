import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { clearAccessToken, setAccessToken } from '@/api';
import { DocumentList } from './DocumentList';

const row = {
  id: 'd1',
  filename: 'handbook.pdf',
  mime_type: 'application/pdf',
  size_bytes: 1,
  collection_id: 'col-1',
  owner_id: 'u1',
  status: 'ready',
  chunk_count: 1,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};
function setup(status = 'ready') {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
    async () =>
      new Response(JSON.stringify({ items: [{ ...row, status }], next_cursor: null }), {
        headers: { 'Content-Type': 'application/json' },
      }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = render(<DocumentList collectionId="col-1" onOpen={() => {}} />, { wrapper });
  return {
    ...view,
    spy,
    urls: () => spy.mock.calls.map(([input]) => new URL(String(input), 'http://localhost')),
  };
}
async function tick(ms = 1) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}
function change(value: string) {
  fireEvent.change(screen.getByRole('searchbox'), { target: { value } });
}
beforeEach(() => {
  vi.useFakeTimers();
  setAccessToken('jwt');
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('filename request debounce (#530)', () => {
  it('updates eight typed characters immediately but sends just one settled request', async () => {
    const { spy, urls } = setup();
    await tick();
    expect(spy).toHaveBeenCalledTimes(1);
    let value = '';
    for (const letter of 'handbook') {
      value += letter;
      change(value);
      expect(screen.getByRole('searchbox')).toHaveValue(value);
      await tick(20);
    }
    const during = spy.mock.calls.length - 1;
    await tick(300);
    const filtered = urls().filter((url) => url.searchParams.has('q'));
    console.info(
      `filename burst: during=${during}, settled=${filtered.length}, q=${filtered.map((url) => url.searchParams.get('q')).join(',')}`,
    );
    expect(during).toBe(0);
    expect(filtered.map((url) => url.searchParams.get('q'))).toEqual(['handbook']);
    expect(screen.getByText('handbook.pdf')).toBeInTheDocument();
  });

  it('clears immediately and never sends a pending prefix', async () => {
    const { urls } = setup();
    await tick();
    change('hand');
    await tick(300);
    change('stale');
    await tick(20);
    change('   ');
    await tick();
    expect(urls().at(-1)?.searchParams.get('q')).toBeNull();
    await tick(300);
    expect(urls().some((url) => url.searchParams.get('q') === 'stale')).toBe(false);
  });

  it('status changes flush the current text once and cancel its old timer', async () => {
    const { urls, spy } = setup();
    await tick();
    change('hand');
    await tick(20);
    fireEvent.click(screen.getByRole('button', { name: 'Failed' }));
    await tick();
    expect(urls().at(-1)?.searchParams.get('q')).toBe('hand');
    expect(urls().at(-1)?.searchParams.get('status')).toBe('failed');
    const count = spy.mock.calls.length;
    await tick(300);
    expect(spy).toHaveBeenCalledTimes(count);
    expect(urls().filter((url) => url.searchParams.get('q') === 'hand')).toHaveLength(1);
  });

  it('collection changes never dispatch pending text to the old collection', async () => {
    const { urls, rerender, spy } = setup();
    await tick();
    change('hand');
    await tick(20);
    rerender(<DocumentList collectionId="col-2" onOpen={() => {}} />);
    await tick();
    expect(urls().at(-1)?.searchParams.get('collection_id')).toBe('col-2');
    expect(urls().at(-1)?.searchParams.get('q')).toBe('hand');
    const count = spy.mock.calls.length;
    await tick(300);
    expect(spy).toHaveBeenCalledTimes(count);
    expect(
      urls().some(
        (url) =>
          url.searchParams.get('q') === 'hand' && url.searchParams.get('collection_id') === 'col-1',
      ),
    ).toBe(false);
  });

  it('unmount cancels a pending filename request', async () => {
    const { spy, unmount } = setup();
    await tick();
    change('stale');
    await tick(20);
    unmount();
    await tick(300);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('keeps ingestion polling active during typing, then stops for settled rows', async () => {
    const { spy } = setup('processing');
    await tick();
    change('hand');
    await tick(300);
    expect(spy).toHaveBeenCalledTimes(2);
    await tick(2500);
    expect(spy).toHaveBeenCalledTimes(3);
    spy.mockImplementation(
      async () =>
        new Response(JSON.stringify({ items: [row], next_cursor: null }), {
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    await tick(2500);
    expect(spy).toHaveBeenCalledTimes(4);
    await tick(5000);
    expect(spy).toHaveBeenCalledTimes(4);
  });
});

it('keeps previous rows while filtering and ignores a late cleared response', async () => {
  const { spy } = setup();
  await tick();
  let resolve!: (response: Response) => void;
  const pending = new Promise<Response>((r) => {
    resolve = r;
  });
  spy.mockImplementationOnce(() => pending);
  change('old');
  await tick(300);
  expect(screen.getByText('handbook.pdf')).toBeInTheDocument();
  expect(screen.queryByText(/loading documents/i)).not.toBeInTheDocument();
  const signal = spy.mock.calls.at(-1)?.[1]?.signal;
  change('');
  await tick();
  expect(signal?.aborted).toBe(true);
  await act(async () =>
    resolve(
      new Response(
        JSON.stringify({ items: [{ ...row, filename: 'stale.pdf' }], next_cursor: null }),
        { headers: { 'Content-Type': 'application/json' } },
      ),
    ),
  );
  await tick();
  expect(screen.queryByText('stale.pdf')).not.toBeInTheDocument();
  expect(screen.getByText('handbook.pdf')).toBeInTheDocument();
});
