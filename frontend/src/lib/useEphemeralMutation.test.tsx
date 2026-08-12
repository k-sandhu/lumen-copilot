import type { PropsWithChildren } from 'react';
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearCredentialDrafts } from './credentialLifecycle';
import { useEphemeralMutation } from './useEphemeralMutation';

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

afterEach(() => onlineManager.setOnline(true));

describe('useEphemeralMutation identity boundary', () => {
  it('drops queued credential variables before they can run as another principal', async () => {
    onlineManager.setOnline(false);
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const request = vi.fn(async (_body: { secret: string }) => undefined);
    const { result } = renderHook(
      () =>
        useEphemeralMutation({
          mutationFn: request,
        }),
      { wrapper: wrapper(queryClient) },
    );

    act(() => result.current.submit({ secret: 'queued-persona-a-secret' }));
    await waitFor(() => expect(result.current.isPaused).toBe(true));

    act(() => clearCredentialDrafts());
    onlineManager.setOnline(true);
    await act(() => queryClient.resumePausedMutations());

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(request).not.toHaveBeenCalled();
    expect(JSON.stringify(queryClient.getMutationCache().getAll())).not.toContain(
      'queued-persona-a-secret',
    );
  });

  it('aborts an already-dispatched credential request on identity change', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    let requestSignal: AbortSignal | undefined;
    const request = vi.fn(
      async (_body: { secret: string }, { signal }: { signal: AbortSignal }) =>
        new Promise<void>((_resolve, reject) => {
          requestSignal = signal;
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );
    const { result } = renderHook(
      () =>
        useEphemeralMutation({
          mutationFn: request,
        }),
      { wrapper: wrapper(queryClient) },
    );

    act(() => result.current.submit({ secret: 'in-flight-persona-a-secret' }));
    await waitFor(() => expect(request).toHaveBeenCalledOnce());

    act(() => clearCredentialDrafts());

    expect(requestSignal?.aborted).toBe(true);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(JSON.stringify(queryClient.getMutationCache().getAll())).not.toContain(
      'in-flight-persona-a-secret',
    );
  });
});
