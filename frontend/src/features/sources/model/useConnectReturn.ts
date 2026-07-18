/**
 * useConnectReturn (#455, ADR-0019 §1) — handles the OAuth callback's redirect
 * back onto the SPA sources route. The backend's `oauthCallback` ALWAYS 302s to
 * this route with the frozen query contract (contracts §oauthCallback):
 *
 *   success — ?connect=ok&source={sourceId}
 *   failure — ?connect=error&reason={expired|denied|provider_error|failed}
 *
 * The hook parses the params ONCE, captures the outcome into local state,
 * CLEANS the query string (a replace navigation, so refresh/back never
 * re-announces a stale outcome), and on success invalidates the sources list so
 * the grid reflects the now-`pending` source immediately. Components render the
 * outcome as a dismissible banner; an unknown `reason` falls back to the
 * generic failure copy — never a blank state.
 *
 * SECURITY: the cleanup is an ALLOWLIST — the sources route carries no other
 * query state, so the ENTIRE query string is cleared, not just the three known
 * params. The redirect query is attacker-influenceable, and deleting only
 * known keys would let injected `code`/`state`/token-like extras survive in
 * the address bar and history. Nothing from the query is ever persisted.
 */
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { ConnectErrorReason } from './types';
import { parseConnectErrorReason } from './presentation';
import { sourcesKey } from './queries';

/** The parsed outcome of an OAuth consent round-trip. */
export type ConnectReturn =
  | { kind: 'ok'; sourceId: string | null }
  | { kind: 'error'; reason: ConnectErrorReason | null };

export function useConnectReturn(): {
  /** The captured outcome, or null when the route carried no connect params. */
  result: ConnectReturn | null;
  /** Clear the banner. */
  dismiss: () => void;
} {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [result, setResult] = useState<ConnectReturn | null>(null);

  const connect = searchParams.get('connect');

  useEffect(() => {
    if (connect === null) return;

    if (connect === 'ok') {
      setResult({ kind: 'ok', sourceId: searchParams.get('source') });
      // The source flipped pending_auth → pending server-side; re-read the grid.
      void queryClient.invalidateQueries({ queryKey: sourcesKey });
    } else {
      // Anything not "ok" is the error leg; the reason is narrowed to the
      // closed set (unknown → null → generic failure copy).
      setResult({ kind: 'error', reason: parseConnectErrorReason(searchParams.get('reason')) });
    }

    // Clean the WHOLE query string off the URL (replace, so back/refresh is
    // quiet). Allowlist semantics: this route has no other query state, and a
    // key-by-key delete would leave injected `code`/`state`/token material
    // behind in the address bar/history.
    setSearchParams(new URLSearchParams(), { replace: true });
    // Run only when a connect param is present; searchParams/setSearchParams are
    // re-created per navigation and the cleanup above removes the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect]);

  return { result, dismiss: () => setResult(null) };
}
