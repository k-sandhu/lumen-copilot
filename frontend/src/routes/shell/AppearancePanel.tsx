/**
 * Appearance & preferences panel (issue #134) — the body of the top-bar
 * appearance popover. Exposes the user-controllable axes the design system
 * supports today: theme (7), mode (light/dark/system), accent override, and
 * density. Every change applies instantly and persists per-device (via
 * useAppearance); because the token layer is unified (#130), the whole app — this
 * panel included — re-skins live, which is its own preview. The compact preview
 * row gives immediate feedback for the axes (accent/density) while the panel
 * covers the screen.
 *
 * A11y: each axis is a labelled group; options are toggle buttons carrying
 * aria-pressed, so the current selection is announced.
 */
import type { CSSProperties, ReactNode } from 'react';
import { Icon, THEMES, ACCENTS, DENSITIES, type AppearanceApi, type ThemeMode } from '@/ui';
import type { IconName } from '@/ui';
import './appearance.css';

const MODE_OPTIONS: Array<{ value: ThemeMode; label: string; icon: IconName }> = [
  { value: 'light', label: 'Light', icon: 'sun' },
  { value: 'dark', label: 'Dark', icon: 'moon' },
  { value: 'system', label: 'System', icon: 'monitor' },
];

interface AppearancePanelProps {
  /** The appearance api (lifted to the trigger so the pill label stays in sync). */
  appearance: AppearanceApi;
  /**
   * Optional account-level preference controls rendered above the (device-local)
   * appearance axes — e.g. the default-model picker (issue #167). Kept a slot so
   * the panel stays a pure, server-state-free presentational component.
   */
  children?: ReactNode;
}

export function AppearancePanel({ appearance: a, children }: AppearancePanelProps) {
  return (
    <div className="lc-aps">
      {children}

      {/* THEME */}
      <div className="lc-aps__section">
        <div className="lc-aps__label">
          <Icon name="sparkles" />
          Theme
        </div>
        <div className="lc-aps__themes" role="group" aria-label="Theme">
          {THEMES.map((t) => {
            const sw = a.resolved === 'dark' ? t.dark : t.light;
            const active = a.theme === t.id;
            const style = {
              '--p-bg': sw.bg,
              '--p-sf': sw.surface,
              '--p-bd': sw.border,
              '--p-ac': sw.accent,
            } as CSSProperties;
            return (
              <button
                key={t.id}
                type="button"
                className={`lc-aps-theme${active ? ' is-active' : ''}`}
                aria-pressed={active}
                onClick={() => a.setTheme(t.id)}
                style={style}
              >
                <span className="lc-aps-theme__swatch">
                  <span className="lc-aps-theme__card" />
                  <span className="lc-aps-theme__dot" />
                </span>
                <span className="lc-aps-theme__name">
                  {active ? <Icon name="check" /> : null}
                  {t.label}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* MODE */}
      <div className="lc-aps__section">
        <div className="lc-aps__label">Mode</div>
        <div className="lc-aps__seg" role="group" aria-label="Mode">
          {MODE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={a.mode === opt.value ? 'is-active' : undefined}
              aria-pressed={a.mode === opt.value}
              onClick={() => a.setMode(opt.value)}
            >
              <Icon name={opt.icon} />
              <span>{opt.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* ACCENT */}
      <div className="lc-aps__section">
        <div className="lc-aps__label">
          Accent
          <button
            type="button"
            className="lc-aps__reset"
            onClick={() => a.resetAccent()}
            disabled={a.accent === null}
          >
            Reset
          </button>
        </div>
        <div className="lc-aps__accents" role="group" aria-label="Accent color">
          {ACCENTS.map((sw) => {
            const active = a.accent === sw.rgb;
            return (
              <button
                key={sw.id}
                type="button"
                className={`lc-aps-accent${active ? ' is-active' : ''}`}
                aria-pressed={active}
                aria-label={sw.label}
                title={sw.label}
                onClick={() => a.setAccent(sw.rgb)}
                style={{ '--sw': sw.hex } as CSSProperties}
              />
            );
          })}
        </div>
      </div>

      {/* DENSITY */}
      <div className="lc-aps__section">
        <div className="lc-aps__label">
          <Icon name="sliders" />
          Density
        </div>
        <div className="lc-aps__seg" role="group" aria-label="Density">
          {DENSITIES.map((d) => (
            <button
              key={d.id}
              type="button"
              className={a.density === d.id ? 'is-active' : undefined}
              aria-pressed={a.density === d.id}
              onClick={() => a.setDensity(d.id)}
            >
              <span>{d.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* LIVE PREVIEW */}
      <div className="lc-aps__section">
        <div className="lc-aps__label">Preview</div>
        <div className="lc-aps__preview">
          <button type="button" className="lc-aps__preview-btn" tabIndex={-1} aria-hidden="true">
            Ask
          </button>
          <span className="lc-aps__preview-chip" aria-hidden="true">
            [1]
          </span>
          <span className="lc-aps__preview-text">Aa · grounded</span>
        </div>
      </div>
    </div>
  );
}
