/**
 * Cross-cutting UI state (Zustand) — ONLY genuine client-side state lives here
 * (frontend/AGENTS.md). Server data belongs in TanStack Query, never mirrored
 * into a store. Today: theme + rail-collapsed.
 */
import { create } from 'zustand';

export type Theme = 'light' | 'dark';

const THEME_KEY = 'beacon.theme';

function prefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  );
}

function initialTheme(): Theme {
  if (typeof localStorage !== 'undefined') {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
  }
  return prefersDark() ? 'dark' : 'light';
}

/** Reflect the theme onto <html class="dark"> so Tailwind dark: variants apply. */
function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.classList.toggle('dark', theme === 'dark');
}

interface UiState {
  theme: Theme;
  railCollapsed: boolean;
  toggleTheme: () => void;
  setTheme: (theme: Theme) => void;
  toggleRail: () => void;
}

export const useUiStore = create<UiState>((set, get) => ({
  theme: initialTheme(),
  railCollapsed: false,
  setTheme: (theme) => {
    applyTheme(theme);
    if (typeof localStorage !== 'undefined') localStorage.setItem(THEME_KEY, theme);
    set({ theme });
  },
  toggleTheme: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark';
    get().setTheme(next);
  },
  toggleRail: () => set((s) => ({ railCollapsed: !s.railCollapsed })),
}));

/** Call once at boot to sync the <html> class with the initial store value. */
export function syncThemeToDom(): void {
  applyTheme(useUiStore.getState().theme);
}
