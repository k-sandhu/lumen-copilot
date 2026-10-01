import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { clearAccessToken, setAccessToken } from '@/api';
import { usePrincipalMutation } from './usePrincipalMutation';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

afterEach(() => {
  onlineManager.setOnline(true);
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('usePrincipalMutation', () => {
  it('blocks hook and per-call callbacks from an old principal after direct replacement', async () => {
    setAccessToken('jwt-persona-a');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const response = deferred<{ sentinel: string }>();
    const hookCallback = vi.fn((data: { sentinel: string }) => {
      queryClient.setQueryData(['detail'], data);
    });
    const callCallback = vi.fn(() => {
      queryClient.setQueryData(['list'], { sentinel: 'persona-a-list' });
    });
    const hookSettled = vi.fn(() => {
      void queryClient.invalidateQueries({ queryKey: ['detail'] });
    });
    const hookError = vi.fn();
    const callSettled = vi.fn(() => {
      void queryClient.refetchQueries({ queryKey: ['list'] });
    });
    const callError = vi.fn();
    const mutationFn = vi.fn(() => response.promise);
    const { result } = renderHook(
      () =>
        usePrincipalMutation({
          mutationFn,
          onSuccess: hookCallback,
          onError: hookError,
          onSettled: hookSettled,
        }),
      { wrapper: wrapper(queryClient) },
    );

    act(() =>
      result.current.mutate(undefined, {
        onSuccess: callCallback,
        onError: callError,
        onSettled: callSettled,
      }),
    );
    await waitFor(() => expect(mutationFn).toHaveBeenCalledOnce());
    act(() => setAccessToken('jwt-persona-b', 'login'));
    queryClient.setQueryData(['detail'], { sentinel: 'persona-b-detail' });
    queryClient.setQueryData(['list'], { sentinel: 'persona-b-list' });
    response.resolve({ sentinel: 'persona-a-detail' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(hookCallback).not.toHaveBeenCalled();
    expect(callCallback).not.toHaveBeenCalled();
    expect(hookError).not.toHaveBeenCalled();
    expect(callError).not.toHaveBeenCalled();
    expect(hookSettled).not.toHaveBeenCalled();
    expect(callSettled).not.toHaveBeenCalled();
    expect(queryClient.getQueryData(['detail'])).toEqual({ sentinel: 'persona-b-detail' });
    expect(queryClient.getQueryData(['list'])).toEqual({ sentinel: 'persona-b-list' });
  });

  it('does not dispatch or settle a paused mutation after the principal changes', async () => {
    setAccessToken('jwt-persona-a');
    onlineManager.setOnline(false);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const mutationFn = vi.fn(async () => ({ sentinel: 'persona-a' }));
    const onSuccess = vi.fn();
    const onSettled = vi.fn();
    const { result } = renderHook(
      () => usePrincipalMutation({ mutationFn, onSuccess, onSettled }),
      { wrapper: wrapper(queryClient) },
    );

    act(() => result.current.mutate(undefined));
    await waitFor(() => expect(result.current.isPaused).toBe(true));
    act(() => setAccessToken('jwt-persona-b', 'login'));
    onlineManager.setOnline(true);
    await queryClient.resumePausedMutations();

    expect(mutationFn).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onSettled).not.toHaveBeenCalled();
  });

  it('preserves all callbacks and server behavior while the principal is unchanged', async () => {
    setAccessToken('jwt-persona-a');
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const mutationFn = vi.fn(async (value: string) => `${value}-response`);
    const hookSuccess = vi.fn();
    const hookSettled = vi.fn();
    const callSuccess = vi.fn();
    const callSettled = vi.fn();
    const { result } = renderHook(
      () => usePrincipalMutation({ mutationFn, onSuccess: hookSuccess, onSettled: hookSettled }),
      { wrapper: wrapper(queryClient) },
    );

    act(() => result.current.mutate('payload', { onSuccess: callSuccess, onSettled: callSettled }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mutationFn).toHaveBeenCalledWith('payload');
    expect(hookSuccess).toHaveBeenCalledWith('payload-response', 'payload', undefined);
    expect(hookSettled).toHaveBeenCalledWith('payload-response', null, 'payload', undefined);
    expect(callSuccess).toHaveBeenCalledWith('payload-response', 'payload', undefined);
    expect(callSettled).toHaveBeenCalledWith('payload-response', null, 'payload', undefined);
  });

  it('rejects mutateAsync data that resolves after a principal switch', async () => {
    setAccessToken('jwt-persona-a');
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const response = deferred<string>();
    const mutationFn = vi.fn(() => response.promise);
    const { result } = renderHook(() => usePrincipalMutation({ mutationFn }), {
      wrapper: wrapper(queryClient),
    });

    let outcome!: Promise<string>;
    act(() => {
      outcome = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(mutationFn).toHaveBeenCalledOnce());
    act(() => setAccessToken('jwt-persona-b', 'login'));
    response.resolve('persona-a-data');

    await expect(outcome).rejects.toMatchObject({
      name: 'SupersededPrincipalMutationError',
    });
  });
});
