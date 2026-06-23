/**
 * Appearance mode for the design-system kit (ADR-0007 §2, curated).
 *
 * v1 ships exactly: the Aurora theme + a light / dark / system mode toggle that
 * is `prefers-color-scheme` aware. The 7-theme picker, accent override, and
 * density/font axes are DEFERRED — the token architecture supports them, but no
 * switching UI ships here.
 *
 * This module owns the resolved appearance and reflects it onto <html>:
 *   • [data-theme="aurora"]        — the single v1 theme identity
 *   • [data-mode="light"|"dark"]   — drives styles/tokens.css (the kit)
 *   • class "dark"                 — drives styles/index.css + Tailwind dark:
 *                                    (existing screens), kept in lock-step so a
 *                                    single toggle moves both systems.
 *
 * Persisted per-device in localStorage. "system" tracks the OS live via a
 * matchMedia listener.
 */

export type ThemeMode = 'light' | 'dark' | 'system';
export type ResolvedMode = 'light' | 'dark';

export const THEME_NAME = 'aurora';
const MODE_KEY = 'lumen.mode';

const DARK_QUERY = '(prefers-color-scheme: dark)';

function canMatchMedia(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function';
}

export function systemPrefersDark(): boolean {
  return canMatchMedia() && window.matchMedia(DARK_QUERY).matches;
}

/** The persisted mode preference, defaulting to "system". */
export function readStoredMode(): ThemeMode {
  if (typeof localStorage === 'undefined') return 'system';
  const stored = localStorage.getItem(MODE_KEY);
  if (stored === 'light' || stored === 'dark' || stored === 'system') return stored;
  return 'system';
}

export function storeMode(mode: ThemeMode): void {
  if (typeof localStorage === 'undefined') return;
  localStorage.setItem(MODE_KEY, mode);
}

/** Collapse the preference to a concrete light|dark, honoring the OS for system. */
export function resolveMode(mode: ThemeMode): ResolvedMode {
  if (mode === 'system') return systemPrefersDark() ? 'dark' : 'light';
  return mode;
}

/**
 * Reflect a resolved appearance onto <html>: theme attr, mode attr, and the
 * legacy `.dark` class so the existing Tailwind screens stay in sync.
 */
export function applyAppearance(resolved: ResolvedMode): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.setAttribute('data-theme', THEME_NAME);
  root.setAttribute('data-mode', resolved);
  root.classList.toggle('dark', resolved === 'dark');
}

/**
 * Subscribe to OS color-scheme changes. The callback fires only while the
 * preference is "system" (the caller passes a getter so we always read the
 * current preference). Returns an unsubscribe fn.
 */
export function subscribeToSystem(onChange: () => void): () => void {
  if (!canMatchMedia()) return () => {};
  const mql = window.matchMedia(DARK_QUERY);
  const handler = (): void => onChange();
  // addEventListener is the modern API; older Safari only has addListener.
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }
  mql.addListener(handler);
  return () => mql.removeListener(handler);
}
