/**
 * useAppearance — the React surface for the appearance engine (issue #134).
 *
 * Reads the shared module-level appearance store via useSyncExternalStore, so the
 * panel, the top-bar trigger, and the command palette always reflect the same
 * state (no per-instance drift). Each setter patches one axis; the store applies
 * it to <html>, persists per-device, and notifies subscribers. While mode is
 * "system", the engine live-tracks the OS color scheme (wired once at boot).
 */
import { useCallback, useSyncExternalStore } from 'react';
import {
  getAppearance,
  resolveMode,
  setAppearance,
  subscribeAppearance,
  type AccentValue,
  type AppearanceState,
  type Density,
  type ThemeName,
} from './appearance';
import type { ResolvedMode, ThemeMode } from './mode';

export interface AppearanceApi extends AppearanceState {
  /** The concrete light|dark currently applied (system resolved against the OS). */
  resolved: ResolvedMode;
  setTheme: (theme: ThemeName) => void;
  setMode: (mode: ThemeMode) => void;
  /** Pass null to clear the override and restore the theme's default accent. */
  setAccent: (accent: AccentValue) => void;
  setDensity: (density: Density) => void;
  /** Restore the theme default accent (alias for setAccent(null)). */
  resetAccent: () => void;
}

export function useAppearance(): AppearanceApi {
  const state = useSyncExternalStore(subscribeAppearance, getAppearance, getAppearance);

  return {
    ...state,
    resolved: resolveMode(state.mode),
    setTheme: useCallback((theme: ThemeName) => setAppearance({ theme }), []),
    setMode: useCallback((mode: ThemeMode) => setAppearance({ mode }), []),
    setAccent: useCallback((accent: AccentValue) => setAppearance({ accent }), []),
    setDensity: useCallback((density: Density) => setAppearance({ density }), []),
    resetAccent: useCallback(() => setAppearance({ accent: null }), []),
  };
}
