/**
 * Appearance engine (issue #134, un-defers ADR-0007 §2).
 *
 * The single owner of the user-controllable appearance axes, reflected onto
 * <html> and persisted per-device. Builds on theme/mode.ts for the mode axis
 * (light/dark/system, prefers-color-scheme aware) and adds:
 *   • THEME   [data-theme]              one of 7 color identities
 *   • ACCENT  inline --c-accent (RGB triple) override, or theme default
 *   • DENSITY inline --fs/--space/--radius multipliers
 *
 * Because the token layer is unified (#130) — Tailwind reads the RGB-triple
 * --c-* tokens and the kit derives its full colors via rgb(var(--c-*)) — setting
 * [data-theme] re-skins BOTH layers, and overriding --c-accent (a triple) flows
 * to every Tailwind *-accent utility AND the derived kit accent shades. Density
 * scales the kit directly (it uses calc(px * var(--fs|--space))) and the rem-based
 * Tailwind screens via the `html { font-size: calc(16px * var(--fs)) }` rule in
 * index.css — so a density change moves the whole UI, not just kit components.
 */
import {
  resolveMode,
  storeMode,
  subscribeToSystem,
  readStoredMode,
  type ResolvedMode,
  type ThemeMode,
} from './mode';

export type ThemeName =
  | 'aurora'
  | 'graphite'
  | 'meridian'
  | 'indigo'
  | 'sunset'
  | 'forest'
  | 'slate';
export type Density = 'compact' | 'cozy' | 'comfortable';
/** An accent override as an RGB triple ("R G B"), or null for the theme default. */
export type AccentValue = string | null;

export interface ThemeSwatch {
  bg: string;
  surface: string;
  border: string;
  text: string;
  accent: string;
}
export interface ThemeDef {
  id: ThemeName;
  label: string;
  light: ThemeSwatch;
  dark: ThemeSwatch;
}

/** Mirrors docs/wireframes/assets/app.js THEMES — used for the gallery previews. */
export const THEMES: ThemeDef[] = [
  {
    id: 'aurora',
    label: 'Aurora',
    light: {
      bg: '#eef1f8',
      surface: '#ffffff',
      border: '#e5e8f0',
      text: '#14161f',
      accent: '#4f46e5',
    },
    dark: {
      bg: '#0c0e16',
      surface: '#161a27',
      border: '#272d42',
      text: '#e9ebf6',
      accent: '#818cf8',
    },
  },
  {
    id: 'graphite',
    label: 'Graphite',
    light: {
      bg: '#f3f4f6',
      surface: '#ffffff',
      border: '#e3e5ea',
      text: '#15171c',
      accent: '#0284c7',
    },
    dark: {
      bg: '#0a0c11',
      surface: '#12151d',
      border: '#232a36',
      text: '#e8ebf2',
      accent: '#38bdf8',
    },
  },
  {
    id: 'meridian',
    label: 'Meridian',
    light: {
      bg: '#f4f1e9',
      surface: '#fdfcf8',
      border: '#e4ddcc',
      text: '#1f1c14',
      accent: '#9c7a1e',
    },
    dark: {
      bg: '#14130c',
      surface: '#1c1a10',
      border: '#322d1b',
      text: '#efeada',
      accent: '#e2b04e',
    },
  },
  {
    id: 'indigo',
    label: 'Indigo',
    light: {
      bg: '#eeeefa',
      surface: '#ffffff',
      border: '#e2e3f3',
      text: '#16161f',
      accent: '#6d28d9',
    },
    dark: {
      bg: '#0d0a1a',
      surface: '#171331',
      border: '#2b2350',
      text: '#ebe9f7',
      accent: '#a78bfa',
    },
  },
  {
    id: 'sunset',
    label: 'Sunset',
    light: {
      bg: '#fbf0ea',
      surface: '#fffaf6',
      border: '#f0ddce',
      text: '#241712',
      accent: '#e0561f',
    },
    dark: {
      bg: '#170d0a',
      surface: '#211210',
      border: '#3a201a',
      text: '#f4e4dd',
      accent: '#fb7a45',
    },
  },
  {
    id: 'forest',
    label: 'Forest',
    light: {
      bg: '#ecf2ec',
      surface: '#ffffff',
      border: '#dce8dc',
      text: '#141914',
      accent: '#15803d',
    },
    dark: {
      bg: '#08110b',
      surface: '#101a13',
      border: '#213328',
      text: '#e6f0e9',
      accent: '#4ade80',
    },
  },
  {
    id: 'slate',
    label: 'Slate',
    light: {
      bg: '#f1f2f4',
      surface: '#ffffff',
      border: '#e2e4e8',
      text: '#16181d',
      accent: '#475569',
    },
    dark: {
      bg: '#0b0d10',
      surface: '#14171c',
      border: '#252a33',
      text: '#e7eaef',
      accent: '#94a3b8',
    },
  },
];

export interface AccentDef {
  id: string;
  label: string;
  /** RGB triple, applied as the inline --c-accent override. */
  rgb: string;
  /** Hex for the swatch's own background in the panel. */
  hex: string;
}

/** Mirrors docs/wireframes/assets/app.js ACCENTS (as triples for --c-accent). */
export const ACCENTS: AccentDef[] = [
  { id: 'blue', label: 'Blue', rgb: '37 99 235', hex: '#2563eb' },
  { id: 'violet', label: 'Violet', rgb: '124 58 237', hex: '#7c3aed' },
  { id: 'teal', label: 'Teal', rgb: '20 184 166', hex: '#14b8a6' },
  { id: 'green', label: 'Green', rgb: '34 197 94', hex: '#22c55e' },
  { id: 'red', label: 'Red', rgb: '239 68 68', hex: '#ef4444' },
  { id: 'orange', label: 'Orange', rgb: '249 115 22', hex: '#f97316' },
  { id: 'pink', label: 'Pink', rgb: '236 72 153', hex: '#ec4899' },
];

export interface DensityDef {
  id: Density;
  label: string;
  fs: number;
  space: number;
  radius: number;
}

/** Mirrors docs/wireframes/assets/app.js DENS. */
const DENSITY_PRESETS: Record<Density, DensityDef> = {
  compact: { id: 'compact', label: 'Compact', fs: 0.92, space: 0.8, radius: 0.9 },
  cozy: { id: 'cozy', label: 'Cozy', fs: 1, space: 1, radius: 1 },
  comfortable: { id: 'comfortable', label: 'Comfortable', fs: 1.08, space: 1.28, radius: 1.1 },
};
export const DENSITIES: DensityDef[] = [
  DENSITY_PRESETS.compact,
  DENSITY_PRESETS.cozy,
  DENSITY_PRESETS.comfortable,
];

export interface AppearanceState {
  theme: ThemeName;
  mode: ThemeMode;
  accent: AccentValue;
  density: Density;
}

const THEME_KEY = 'lumen.themeName';
const ACCENT_KEY = 'lumen.accent';
const DENSITY_KEY = 'lumen.density';
// mode reuses theme/mode.ts's 'lumen.mode' key, so the two stay consistent.

const THEME_IDS = THEMES.map((t) => t.id);
const DENSITY_IDS = DENSITIES.map((d) => d.id);

function readStored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  if (typeof localStorage === 'undefined') return fallback;
  const v = localStorage.getItem(key);
  return v && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

/** The persisted appearance, with sane defaults (Aurora · system · cozy). */
export function readAppearance(): AppearanceState {
  const accentRaw = typeof localStorage === 'undefined' ? null : localStorage.getItem(ACCENT_KEY);
  return {
    theme: readStored<ThemeName>(THEME_KEY, THEME_IDS, 'aurora'),
    mode: readStoredMode(),
    accent: accentRaw && /^\d{1,3} \d{1,3} \d{1,3}$/.test(accentRaw) ? accentRaw : null,
    density: readStored<Density>(DENSITY_KEY, DENSITY_IDS, 'cozy'),
  };
}

export function storeAppearance(state: AppearanceState): void {
  if (typeof localStorage === 'undefined') return;
  localStorage.setItem(THEME_KEY, state.theme);
  storeMode(state.mode);
  localStorage.setItem(DENSITY_KEY, state.density);
  if (state.accent) localStorage.setItem(ACCENT_KEY, state.accent);
  else localStorage.removeItem(ACCENT_KEY);
}

/** Reflect a full appearance state onto <html> (theme, mode, accent, density). */
export function applyAppearance(state: AppearanceState): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  const resolved: ResolvedMode = resolveMode(state.mode);

  root.setAttribute('data-theme', state.theme);
  root.setAttribute('data-mode', resolved);
  root.classList.toggle('dark', resolved === 'dark');

  if (state.accent) {
    // Override the triple so BOTH Tailwind *-accent utilities and the derived
    // kit accent shades follow; white reads on every override swatch.
    root.style.setProperty('--c-accent', state.accent);
    root.style.setProperty('--accent-contrast', '#ffffff');
  } else {
    root.style.removeProperty('--c-accent');
    root.style.removeProperty('--accent-contrast');
  }

  const d = DENSITY_PRESETS[state.density];
  root.style.setProperty('--fs', String(d.fs));
  root.style.setProperty('--space', String(d.space));
  root.style.setProperty('--radius', String(d.radius));
}

// ── shared store ──────────────────────────────────────────────────────────
// A single module-level state so every consumer (the panel, the top-bar trigger,
// the command palette) stays in lock-step — without coupling the kit to the app's
// Zustand store. Read via useAppearance (useSyncExternalStore).
let current: AppearanceState | null = null;
const listeners = new Set<() => void>();
let systemWired = false;

function wireSystemOnce(): void {
  if (systemWired || typeof window === 'undefined') return;
  systemWired = true;
  // When the OS scheme flips and the preference is "system", re-resolve + notify.
  subscribeToSystem(() => {
    if (current?.mode === 'system') {
      applyAppearance(current);
      listeners.forEach((l) => l());
    }
  });
}

/** The current shared appearance snapshot (lazily seeded from localStorage). */
export function getAppearance(): AppearanceState {
  if (!current) current = readAppearance();
  return current;
}

/** Patch one or more axes: update the shared state, apply to <html>, persist, notify. */
export function setAppearance(patch: Partial<AppearanceState>): void {
  current = { ...getAppearance(), ...patch };
  applyAppearance(current);
  storeAppearance(current);
  listeners.forEach((l) => l());
}

export function subscribeAppearance(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Apply the persisted appearance. Call once at boot, before first paint. */
export function applyStoredAppearance(): AppearanceState {
  current = readAppearance();
  applyAppearance(current);
  wireSystemOnce();
  return current;
}

export { subscribeToSystem, resolveMode };
