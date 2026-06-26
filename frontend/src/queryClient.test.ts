/**
 * queryClient defaults (#166) — a single retry that backs off exponentially
 * (capped), instead of retrying immediately with no delay. The backoff curve
 * mirrors the WS reconnect delay (`api/ws.ts`).
 */
import { describe, it, expect } from 'vitest';
import { queryClient, retryDelay } from './queryClient';

describe('queryClient retry backoff', () => {
  it('retries once', () => {
    expect(queryClient.getDefaultOptions().queries?.retry).toBe(1);
  });

  it('wires retryDelay into the query defaults', () => {
    expect(queryClient.getDefaultOptions().queries?.retryDelay).toBe(retryDelay);
  });

  it('grows exponentially with the attempt number', () => {
    expect(retryDelay(0)).toBe(1_000);
    expect(retryDelay(1)).toBe(2_000);
    expect(retryDelay(2)).toBe(4_000);
    // Strictly increasing until the ceiling.
    expect(retryDelay(1)).toBeGreaterThan(retryDelay(0));
    expect(retryDelay(2)).toBeGreaterThan(retryDelay(1));
  });

  it('caps the delay at the 30s ceiling', () => {
    expect(retryDelay(20)).toBe(30_000);
    expect(retryDelay(100)).toBe(30_000);
  });
});
