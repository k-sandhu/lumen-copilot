import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useThemeMode } from './useThemeMode';

function stubMatchMedia(prefersDark: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    matches: prefersDark,
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    addListener: (cb: () => void) => listeners.add(cb),
    removeListener: (cb: () => void) => listeners.delete(cb),
    dispatchEvent: () => false,
  } as unknown as MediaQueryList;
  window.matchMedia = vi.fn().mockReturnValue(mql);
  return { emit: () => listeners.forEach((l) => l()) };
}

describe('useThemeMode', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-mode');
    document.documentElement.classList.remove('dark');
  });
  afterEach(() => vi.restoreAllMocks());

  it('applies the persisted preference to <html> on mount', () => {
    stubMatchMedia(false);
    localStorage.setItem('lumen.mode', 'dark');
    const { result } = renderHook(() => useThemeMode());
    expect(result.current.mode).toBe('dark');
    expect(result.current.resolved).toBe('dark');
    expect(document.documentElement.getAttribute('data-mode')).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('persists + re-applies when the mode changes', () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useThemeMode());
    act(() => result.current.setMode('light'));
    expect(localStorage.getItem('lumen.mode')).toBe('light');
    expect(document.documentElement.getAttribute('data-mode')).toBe('light');
  });

  it('follows the OS live while the preference is "system"', () => {
    const media = stubMatchMedia(false);
    const { result } = renderHook(() => useThemeMode());
    expect(result.current.resolved).toBe('light');
    // OS flips to dark
    (window.matchMedia as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      matches: true,
      media: '(prefers-color-scheme: dark)',
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    } as unknown as MediaQueryList);
    act(() => media.emit());
    expect(result.current.resolved).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('does not track the OS once an explicit mode is chosen', () => {
    const media = stubMatchMedia(false);
    const { result } = renderHook(() => useThemeMode());
    act(() => result.current.setMode('light'));
    // Even if the OS would flip, an explicit light preference stays light.
    act(() => media.emit());
    expect(result.current.resolved).toBe('light');
  });
});
