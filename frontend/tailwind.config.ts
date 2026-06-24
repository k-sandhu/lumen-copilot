import type { Config } from 'tailwindcss';

// Design tokens are kept minimal here; the chat UI extends this deliberately.
// Dark mode is class-based so the Zustand theme store can toggle it.
const config: Config = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // CSS-variable-backed tokens so themes swap without re-tailwinding.
        // Backed by the RGB-triple --c-* tokens in styles/tokens.css (the single
        // source of truth per theme×mode). The kit's full-color vars (--surface,
        // --accent, …) are DERIVED from these same triples, so both layers stay
        // in lock-step and never collide (issue #130). The color KEYS below are
        // unchanged, so every existing `bg-surface`/`text-accent` utility still
        // works — only the backing variable name moved to --c-*.
        surface: 'rgb(var(--c-surface) / <alpha-value>)',
        'surface-muted': 'rgb(var(--c-surface-muted) / <alpha-value>)',
        border: 'rgb(var(--c-border) / <alpha-value>)',
        foreground: 'rgb(var(--c-foreground) / <alpha-value>)',
        'foreground-muted': 'rgb(var(--c-foreground-muted) / <alpha-value>)',
        accent: 'rgb(var(--c-accent) / <alpha-value>)',
        ok: 'rgb(var(--c-ok) / <alpha-value>)',
        warn: 'rgb(var(--c-warn) / <alpha-value>)',
        danger: 'rgb(var(--c-danger) / <alpha-value>)',
      },
    },
  },
  plugins: [],
};

export default config;
