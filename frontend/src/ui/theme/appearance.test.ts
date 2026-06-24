/**
 * Appearance engine tests (issue #134) — persistence, defaults, validation, the
 * <html> reflection (theme/mode/accent/density), and the shared store.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  ACCENTS,
  DENSITIES,
  THEMES,
  applyAppearance,
  applyStoredAppearance,
  getAppearance,
  readAppearance,
  setAppearance,
  storeAppearance,
  subscribeAppearance,
} from './appearance';

const root = (): HTMLElement => document.documentElement;

beforeEach(() => {
  localStorage.clear();
  root().removeAttribute('data-theme');
  root().removeAttribute('data-mode');
  root().removeAttribute('style');
  root().className = '';
  applyStoredAppearance(); // reset the shared store to persisted defaults
});

describe('appearance catalog', () => {
  it('ships the 7 themes, 7 accents, and 3 densities from the wireframe', () => {
    expect(THEMES.map((t) => t.id)).toEqual([
      'aurora',
      'graphite',
      'meridian',
      'indigo',
      'sunset',
      'forest',
      'slate',
    ]);
    expect(ACCENTS).toHaveLength(7);
    expect(ACCENTS.every((a) => /^\d{1,3} \d{1,3} \d{1,3}$/.test(a.rgb))).toBe(true);
    expect(DENSITIES.map((d) => d.id)).toEqual(['compact', 'cozy', 'comfortable']);
  });
});

describe('persistence', () => {
  it('defaults to aurora / system / cozy with no stored prefs', () => {
    expect(readAppearance()).toMatchObject({
      theme: 'aurora',
      mode: 'system',
      accent: null,
      density: 'cozy',
    });
  });

  it('persists and round-trips every axis', () => {
    storeAppearance({ theme: 'forest', mode: 'dark', accent: '34 197 94', density: 'compact' });
    expect(readAppearance()).toMatchObject({
      theme: 'forest',
      mode: 'dark',
      accent: '34 197 94',
      density: 'compact',
    });
  });

  it('clears the accent key when reset to the theme default', () => {
    storeAppearance({ theme: 'aurora', mode: 'light', accent: '34 197 94', density: 'cozy' });
    storeAppearance({ theme: 'aurora', mode: 'light', accent: null, density: 'cozy' });
    expect(localStorage.getItem('lumen.accent')).toBeNull();
  });

  it('ignores a malformed stored accent and an unknown theme', () => {
    localStorage.setItem('lumen.accent', 'red');
    localStorage.setItem('lumen.themeName', 'neon');
    expect(readAppearance().accent).toBeNull();
    expect(readAppearance().theme).toBe('aurora');
  });
});

describe('applyAppearance reflects onto <html>', () => {
  it('sets theme, mode, the dark class, and the density multipliers', () => {
    applyAppearance({ theme: 'forest', mode: 'dark', accent: null, density: 'comfortable' });
    expect(root().getAttribute('data-theme')).toBe('forest');
    expect(root().getAttribute('data-mode')).toBe('dark');
    expect(root().classList.contains('dark')).toBe(true);
    expect(root().style.getPropertyValue('--fs')).toBe('1.08');
    expect(root().style.getPropertyValue('--space')).toBe('1.28');
    expect(root().style.getPropertyValue('--radius')).toBe('1.1');
  });

  it('overrides --c-accent (a triple, so both layers follow) and clears it on reset', () => {
    applyAppearance({ theme: 'aurora', mode: 'light', accent: '34 197 94', density: 'cozy' });
    expect(root().style.getPropertyValue('--c-accent')).toBe('34 197 94');
    expect(root().style.getPropertyValue('--accent-contrast')).toBe('#ffffff');

    applyAppearance({ theme: 'aurora', mode: 'light', accent: null, density: 'cozy' });
    expect(root().style.getPropertyValue('--c-accent')).toBe('');
    expect(root().style.getPropertyValue('--accent-contrast')).toBe('');
  });
});

describe('shared store', () => {
  it('updates the snapshot, reflects to <html>, and notifies subscribers', () => {
    const seen: string[] = [];
    const unsub = subscribeAppearance(() => seen.push(getAppearance().theme));

    setAppearance({ theme: 'sunset' });

    expect(getAppearance().theme).toBe('sunset');
    expect(root().getAttribute('data-theme')).toBe('sunset');
    expect(seen).toContain('sunset');

    setAppearance({ density: 'compact' });
    expect(getAppearance()).toMatchObject({ theme: 'sunset', density: 'compact' });

    unsub();
  });
});
