import { describe, it, expect, beforeEach } from 'vitest';
import { useAuthStore } from './authStore';
import { clearAccessToken, setAccessToken } from '@/api';

function reset() {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
}

describe('authStore', () => {
  beforeEach(reset);

  it('starts in the unknown (bootstrapping) status', () => {
    expect(useAuthStore.getState().status).toBe('unknown');
  });

  it('markAuthenticated flips status to authenticated', () => {
    useAuthStore.getState().markAuthenticated();
    expect(useAuthStore.getState().status).toBe('authenticated');
  });

  it('markUnauthenticated flips status to unauthenticated', () => {
    useAuthStore.getState().markAuthenticated();
    useAuthStore.getState().markUnauthenticated();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
  });

  it('reacts to the api token holder: clearing the token marks unauthenticated', () => {
    // The store subscribes to token changes at module load. A failed silent
    // refresh clears the token, which must route the user back to login (AC-4).
    setAccessToken('jwt');
    useAuthStore.getState().markAuthenticated();

    clearAccessToken();

    expect(useAuthStore.getState().status).toBe('unauthenticated');
  });
});
