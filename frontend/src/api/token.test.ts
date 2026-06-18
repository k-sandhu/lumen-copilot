import { describe, it, expect, beforeEach } from 'vitest';
import {
  getAccessToken,
  setAccessToken,
  clearAccessToken,
  hasAccessToken,
  subscribeToken,
} from './token';

describe('access-token holder', () => {
  beforeEach(() => clearAccessToken());

  it('starts empty', () => {
    expect(getAccessToken()).toBeNull();
    expect(hasAccessToken()).toBe(false);
  });

  it('stores and returns a token in memory', () => {
    setAccessToken('jwt-123');
    expect(getAccessToken()).toBe('jwt-123');
    expect(hasAccessToken()).toBe(true);
  });

  it('clears the token', () => {
    setAccessToken('jwt-123');
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
    expect(hasAccessToken()).toBe(false);
  });

  it('notifies subscribers on change and stops after unsubscribe', () => {
    const seen: Array<string | null> = [];
    const unsubscribe = subscribeToken((t) => seen.push(t));

    setAccessToken('a');
    clearAccessToken();
    unsubscribe();
    setAccessToken('b');

    expect(seen).toEqual(['a', null]);
  });
});
