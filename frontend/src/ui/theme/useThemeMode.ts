/**
 * useThemeMode — the React surface for the kit's light/dark/system mode.
 *
 * Self-contained (no external store), so the kit stays opt-in and does not
 * depend on the app's existing `stores/ui.ts`. On mount it applies the persisted
 * preference to <html> and, while the preference is "system", live-tracks the OS
 * via `prefers-color-scheme`. Switching mode persists and re-applies.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  applyAppearance,
  readStoredMode,
  resolveMode,
  storeMode,
  subscribeToSystem,
  type ResolvedMode,
  type ThemeMode,
} from './mode';

export interface ThemeModeApi {
  /** The user's preference: light | dark | system. */
  mode: ThemeMode;
  /** The concrete appearance currently applied (system resolved against the OS). */
  resolved: ResolvedMode;
  /** Set the preference; persists + re-applies to <html>. */
  setMode: (mode: ThemeMode) => void;
}

export function useThemeMode(): ThemeModeApi {
  const [mode, setModeState] = useState<ThemeMode>(() => readStoredMode());
  const [resolved, setResolved] = useState<ResolvedMode>(() => resolveMode(mode));

  // Apply on mount + whenever the preference changes.
  useEffect(() => {
    const next = resolveMode(mode);
    setResolved(next);
    applyAppearance(next);
  }, [mode]);

  // While "system", follow the OS live.
  useEffect(() => {
    if (mode !== 'system') return;
    return subscribeToSystem(() => {
      const next = resolveMode('system');
      setResolved(next);
      applyAppearance(next);
    });
  }, [mode]);

  const setMode = useCallback((next: ThemeMode) => {
    storeMode(next);
    setModeState(next);
  }, []);

  return { mode, resolved, setMode };
}
