import { useCallback, useEffect, useRef } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useCredentialClearer } from '@/lib/credentialLifecycle';

interface EphemeralMutationContext {
  signal: AbortSignal;
}

interface EphemeralMutationOptions<TData, TError, TVariables> {
  mutationFn: (variables: TVariables, context: EphemeralMutationContext) => Promise<TData>;
  onSuccess?: (data: TData) => void;
  onError?: (error: TError) => void;
  onSettled?: (data: TData | undefined, error: TError | null) => void;
}

interface EphemeralSubmitOptions<TData, TError> {
  onSuccess?: (data: TData) => void;
  onError?: (error: TError) => void;
  onSettled?: (data: TData | undefined, error: TError | null) => void;
}

const CLEARED = Symbol('cleared credential variables');

interface PendingVariables<TVariables> {
  value: TVariables | typeof CLEARED;
}

/**
 * Run a credential-bearing mutation without putting its variables in TanStack's
 * MutationCache (and therefore Query Devtools). TanStack sees only an opaque
 * per-hook sequence number; the real variables live in a short-lived ref until
 * the mutation starts and are dropped when the request settles.
 */
export function useEphemeralMutation<TData, TError = unknown, TVariables = void>({
  mutationFn,
  onSuccess,
  onError,
  onSettled,
}: EphemeralMutationOptions<TData, TError, TVariables>) {
  const nextToken = useRef(0);
  const pending = useRef(new Map<number, PendingVariables<TVariables>>());
  const active = useRef(new Map<number, AbortController>());

  const clearVariables = useCallback(() => {
    for (const holder of pending.current.values()) holder.value = CLEARED;
    pending.current.clear();
    for (const controller of active.current.values()) controller.abort();
    active.current.clear();
  }, []);

  useCredentialClearer(clearVariables);

  useEffect(() => () => clearVariables(), [clearVariables]);

  const mutation = useMutation<TData, TError, number>({
    mutationFn: async (token) => {
      const holder = pending.current.get(token);
      pending.current.delete(token);
      if (!holder || holder.value === CLEARED) {
        throw new Error('Credential submission is no longer available.');
      }

      const variables = holder.value;
      const controller = new AbortController();
      active.current.set(token, controller);

      try {
        return await mutationFn(variables, { signal: controller.signal });
      } finally {
        holder.value = CLEARED;
        active.current.delete(token);
      }
    },
    onSuccess: (data) => onSuccess?.(data),
    onError: (error) => onError?.(error),
    onSettled: (data, error) => onSettled?.(data, error),
  });

  function submit(
    variables: TVariables,
    callbacks: EphemeralSubmitOptions<TData, TError> = {},
  ): void {
    const token = ++nextToken.current;
    pending.current.set(token, { value: variables });
    mutation.mutate(token, {
      onSuccess: (data) => callbacks.onSuccess?.(data),
      onError: (error) => callbacks.onError?.(error),
      onSettled: (data, error) => callbacks.onSettled?.(data, error),
    });
  }

  return { ...mutation, submit };
}
