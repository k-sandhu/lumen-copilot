/**
 * RouteGuard (AC-3): unauthenticated users see the login screen; authenticated
 * users see the app shell (children). While the session is bootstrapping
 * (silent refresh in flight) it shows a loading state — never a login flash.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { RouteGuard } from './RouteGuard';
import { useAuthStore } from '../model/authStore';
import { bootstrapSession, resetBootstrapForTests } from '../model/useBootstrapSession';
import { clearAccessToken, getAccessToken, getAuthIntentGeneration, login } from '@/api';
import { getActiveAuthSlot, setActiveAuthSlot } from '@/api/authSlot';
import { useCredentialClearer } from '@/lib/credentialLifecycle';

function tokenResponse(): Response {
  return new Response(
    JSON.stringify({ access_token: 'jwt', token_type: 'bearer', expires_in: 900 }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

function unauthorized(): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: 'x', status: 401 }), {
    status: 401,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
  resetBootstrapForTests();
});
afterEach(() => vi.restoreAllMocks());

describe('RouteGuard', () => {
  it.each(['success', 'failure'] as const)(
    'Sign in again terminates restoration and rejects late old %s (R5-002)',
    async (lateOutcome) => {
      const slotA = '11111111-1111-4111-8111-111111111111';
      setActiveAuthSlot(slotA);
      let release!: (response: Response) => void;
      let refreshSignal: AbortSignal | undefined;
      let started!: () => void;
      const refreshStarted = new Promise<void>((resolve) => {
        started = resolve;
      });
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        if (String(input).endsWith('/auth/refresh')) {
          refreshSignal = init?.signal ?? undefined;
          started();
          // An uncooperative old response proves generation checks, not just abort.
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        if (String(input).endsWith('/auth/login')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                access_token: 'jwt-persona-b',
                token_type: 'bearer',
                expires_in: 900,
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } },
            ),
          );
        }
        return Promise.resolve(unauthorized());
      });
      const wipeDraft = vi.fn();
      function CredentialHolder() {
        useCredentialClearer(wipeDraft);
        return null;
      }
      const { queryClient } = renderWithQuery(
        <>
          <CredentialHolder />
          <RouteGuard>
            <div>protected shell</div>
          </RouteGuard>
        </>,
      );
      await refreshStarted;
      queryClient.setQueryData(['old-principal'], 'private A data');
      queryClient.getMutationCache().build(
        queryClient,
        {},
        {
          context: undefined,
          data: 'private A result',
          error: null,
          failureCount: 0,
          failureReason: null,
          isPaused: false,
          status: 'success',
          variables: undefined,
          submittedAt: 0,
        },
      );
      const intentBefore = getAuthIntentGeneration();
      fireEvent.click(screen.getByRole('button', { name: /^sign in again$/i }));
      expect(refreshSignal?.aborted).toBe(true);
      expect(getAuthIntentGeneration()).toBeGreaterThan(intentBefore);
      expect(getActiveAuthSlot()).toBeNull();
      expect(getAccessToken()).toBeNull();
      expect(wipeDraft).toHaveBeenCalled();
      expect(queryClient.getQueryCache().getAll()).toEqual([]);
      expect(queryClient.getMutationCache().getAll()).toEqual([]);
      expect(
        screen.getByRole('heading', { name: /sign in to your workspace/i }),
      ).toBeInTheDocument();
      expect(screen.queryByText('protected shell')).not.toBeInTheDocument();
      await act(async () => {
        release(lateOutcome === 'success' ? tokenResponse() : unauthorized());
        await bootstrapSession();
      });
      expect(getAccessToken()).toBeNull();
      expect(getActiveAuthSlot()).toBeNull();
      expect(useAuthStore.getState().status).toBe('unauthenticated');
      await act(async () => {
        await login({ email: 'persona-b@example.test', password: 'b' });
      });
      expect(getAccessToken()).toBe('jwt-persona-b');
      expect(getActiveAuthSlot()).not.toBe(slotA);
      expect(
        fetchSpy.mock.calls.filter(([input]) => String(input).endsWith('/auth/refresh')),
      ).toHaveLength(1);
    },
  );

  it('shows a loading state while bootstrapping (no login flash) (AC-3)', () => {
    // Pending refresh — never resolves during this assertion.
    vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>(() => {}));
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText('protected shell')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^sign in$/i })).not.toBeInTheDocument();
  });

  it('routes unauthenticated users to the login screen (AC-3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized());
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^sign in$/i })).toBeInTheDocument(),
    );
    expect(screen.queryByText('protected shell')).not.toBeInTheDocument();
  });

  it('renders the app shell once authenticated (AC-3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(tokenResponse());
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    await waitFor(() => expect(screen.getByText('protected shell')).toBeInTheDocument());
  });
});
