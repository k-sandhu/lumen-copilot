import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  THEME_NAME,
  readStoredMode,
  storeMode,
  resolveMode,
  applyAppearance,
  systemPrefersDark,
  subscribeToSystem,
} from './mode';

/** Install a matchMedia stub that reports the given prefers-dark value and can
 *  emit a change event to registered listeners. */
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
  return { emit: () => listeners.forEach((l) => l()), size: () => listeners.size };
}

describe('theme/mode', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.removeAttribute('data-mode');
    document.documentElement.classList.remove('dark');
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('defaults the stored preference to "system"', () => {
    expect(readStoredMode()).toBe('system');
  });

  it('round-trips a stored preference', () => {
    storeMode('dark');
    expect(readStoredMode()).toBe('dark');
    expect(localStorage.getItem('lumen.mode')).toBe('dark');
  });

  it('ignores a corrupt stored value', () => {
    localStorage.setItem('lumen.mode', 'neon');
    expect(readStoredMode()).toBe('system');
  });

  it('resolves explicit light/dark unchanged', () => {
    expect(resolveMode('light')).toBe('light');
    expect(resolveMode('dark')).toBe('dark');
  });

  it('resolves "system" against prefers-color-scheme', () => {
    stubMatchMedia(true);
    expect(systemPrefersDark()).toBe(true);
    expect(resolveMode('system')).toBe('dark');
    stubMatchMedia(false);
    expect(resolveMode('system')).toBe('light');
  });

  it('reflects appearance onto <html>: data-theme, data-mode, and the .dark class', () => {
    applyAppearance('dark');
    const root = document.documentElement;
    expect(root.getAttribute('data-theme')).toBe(THEME_NAME);
    expect(root.getAttribute('data-mode')).toBe('dark');
    expect(root.classList.contains('dark')).toBe(true);

    applyAppearance('light');
    expect(root.getAttribute('data-mode')).toBe('light');
    expect(root.classList.contains('dark')).toBe(false);
  });

  it('subscribeToSystem notifies on OS change and unsubscribes cleanly', () => {
    const media = stubMatchMedia(false);
    const onChange = vi.fn();
    const unsub = subscribeToSystem(onChange);
    media.emit();
    expect(onChange).toHaveBeenCalledTimes(1);
    unsub();
    expect(media.size()).toBe(0);
    media.emit();
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
