import { useCallback } from 'react';
import {
  useMutation,
  type MutateOptions,
  type UseMutationOptions,
  type UseMutationResult,
} from '@tanstack/react-query';
import { getPrincipalGeneration } from '@/api';

interface PrincipalVariables<TVariables> {
  generation: number;
  value: TVariables;
}

class SupersededPrincipalMutationError extends Error {
  constructor() {
    super('Mutation belongs to a superseded principal.');
    this.name = 'SupersededPrincipalMutationError';
  }
}

function isCurrent(generation: number): boolean {
  return getPrincipalGeneration() === generation;
}

function guardedCallbacks<TData, TError, TVariables, TContext>(
  generation: number,
  callbacks?: MutateOptions<TData, TError, TVariables, TContext>,
): MutateOptions<TData, TError, PrincipalVariables<TVariables>, TContext> | undefined {
  if (!callbacks) return undefined;
  return {
    onSuccess: (data, variables, context) => {
      if (isCurrent(generation)) callbacks.onSuccess?.(data, variables.value, context);
    },
    onError: (error, variables, context) => {
      if (isCurrent(generation)) callbacks.onError?.(error, variables.value, context);
    },
    onSettled: (data, error, variables, context) => {
      if (isCurrent(generation)) callbacks.onSettled?.(data, error, variables.value, context);
    },
  };
}

/**
 * Principal-scoped replacement for TanStack's raw ``useMutation``.
 *
 * The identity generation is captured when ``mutate``/``mutateAsync`` is
 * invoked, including while an offline mutation is paused. Work for an old
 * generation never reaches the caller's mutation function after resume, and
 * every hook-level or per-call callback is suppressed after an account switch.
 * This keeps old ``setQueryData``/invalidate/refetch callbacks from rebuilding
 * server state after the canonical transition has cleared the QueryClient.
 */
export function usePrincipalMutation<
  TData = unknown,
  TError = Error,
  TVariables = void,
  TContext = unknown,
>(
  options: UseMutationOptions<TData, TError, TVariables, TContext>,
): UseMutationResult<TData, TError, TVariables, TContext> {
  const { mutationFn, onMutate, onSuccess, onError, onSettled, ...rest } = options;
  const mutation = useMutation<TData, TError, PrincipalVariables<TVariables>, TContext>({
    ...rest,
    mutationFn: async (variables) => {
      if (!isCurrent(variables.generation)) throw new SupersededPrincipalMutationError();
      if (!mutationFn) throw new Error('No mutationFn found');
      const data = await mutationFn(variables.value);
      if (!isCurrent(variables.generation)) throw new SupersededPrincipalMutationError();
      return data;
    },
    onMutate: onMutate
      ? async (variables) => {
          if (!isCurrent(variables.generation)) throw new SupersededPrincipalMutationError();
          return onMutate(variables.value);
        }
      : undefined,
    onSuccess: (data, variables, context) => {
      if (isCurrent(variables.generation)) onSuccess?.(data, variables.value, context);
    },
    onError: (error, variables, context) => {
      if (isCurrent(variables.generation)) onError?.(error, variables.value, context);
    },
    onSettled: (data, error, variables, context) => {
      if (isCurrent(variables.generation)) {
        onSettled?.(data, error, variables.value, context);
      }
    },
  });
  const rawMutate = mutation.mutate;
  const rawMutateAsync = mutation.mutateAsync;

  const mutate = useCallback(
    (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) => {
      const generation = getPrincipalGeneration();
      rawMutate({ generation, value: variables }, guardedCallbacks(generation, callbacks));
    },
    [rawMutate],
  );

  const mutateAsync = useCallback(
    (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) => {
      const generation = getPrincipalGeneration();
      return rawMutateAsync(
        { generation, value: variables },
        guardedCallbacks(generation, callbacks),
      );
    },
    [rawMutateAsync],
  );

  return {
    ...mutation,
    variables: mutation.variables?.value,
    mutate,
    mutateAsync,
  } as UseMutationResult<TData, TError, TVariables, TContext>;
}
