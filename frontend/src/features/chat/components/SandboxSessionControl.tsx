import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  closeSandboxSession,
  getSandboxSession,
  resetSandboxSession,
  type SandboxSession,
} from '@/api';
import { usePrincipalMutation } from '@/lib/usePrincipalMutation';

const labels: Record<SandboxSession['status'], string> = {
  not_created: 'Python sandbox: on first use',
  active: 'Python sandbox: active',
  closed: 'Python sandbox: closed',
  error: 'Python sandbox: needs reset',
};

export function SandboxSessionControl({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const [pendingAction, setPendingAction] = useState<'reset' | 'close' | null>(null);
  const key = ['chat', 'sandbox', sessionId] as const;
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => getSandboxSession(sessionId, signal),
    // A run can create/recover the sandbox through the chat stream rather than
    // this control. Poll only non-active states until that server-side transition
    // appears; active lifecycle mutations update/invalidate this cache directly.
    refetchInterval: (current) => (current.state.data?.status === 'active' ? false : 5_000),
  });
  const reset = usePrincipalMutation({
    mutationFn: () => resetSandboxSession(sessionId),
    onSuccess: (value) => {
      queryClient.setQueryData(key, value);
      setPendingAction(null);
    },
  });
  const close = usePrincipalMutation({
    mutationFn: () => closeSandboxSession(sessionId),
    onSuccess: () => {
      queryClient.setQueryData<SandboxSession>(key, (current) => ({
        status: 'closed',
        enabled: current?.enabled ?? true,
        root_access: true,
        sandbox_session_id: current?.sandbox_session_id ?? null,
        generation: current?.generation ?? null,
      }));
      setPendingAction(null);
    },
  });

  if (query.isLoading) {
    return <span className="text-xs text-foreground-muted">Checking Python sandbox…</span>;
  }
  if (query.isError || !query.data) {
    return (
      <button type="button" className="lc-chat__toolbtn" onClick={() => void query.refetch()}>
        Retry sandbox status
      </button>
    );
  }

  const value = query.data;
  if (!value.enabled) {
    return <span className="text-xs text-foreground-muted">Python sandbox: unavailable</span>;
  }
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Python sandbox session">
      <span className="text-xs text-foreground-muted">
        {labels[value.status]}
        {value.generation ? ` · generation ${value.generation}` : ''}
        {value.status === 'active' ? ' · root inside container' : ''}
      </span>
      {pendingAction ? (
        <div role="alert" className="flex items-center gap-2 text-xs">
          <span>
            {pendingAction === 'reset'
              ? 'Reset deletes installed packages, files, and running processes.'
              : 'Closing deletes installed packages, files, and running processes.'}
          </span>
          <button
            type="button"
            className="lc-chat__toolbtn"
            disabled={reset.isPending || close.isPending}
            onClick={() => (pendingAction === 'reset' ? reset.mutate() : close.mutate())}
          >
            {pendingAction === 'reset'
              ? reset.isPending
                ? 'Resetting…'
                : 'Delete state and reset'
              : close.isPending
                ? 'Closing…'
                : 'Delete state and close'}
          </button>
          <button type="button" className="lc-chat__toolbtn" onClick={() => setPendingAction(null)}>
            Keep state
          </button>
        </div>
      ) : (
        <>
          <button
            type="button"
            className="lc-chat__toolbtn"
            onClick={() => setPendingAction('reset')}
          >
            Reset sandbox
          </button>
          {value.status === 'active' ? (
            <button
              type="button"
              className="lc-chat__toolbtn"
              onClick={() => setPendingAction('close')}
            >
              Close sandbox
            </button>
          ) : null}
        </>
      )}
      {reset.isError || close.isError ? (
        <span role="alert" className="text-xs text-danger">
          The sandbox operation failed. Its current state was preserved where possible.
        </span>
      ) : null}
    </div>
  );
}
