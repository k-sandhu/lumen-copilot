/**
 * Appearance control (issue #110) — the top-bar trigger shows the current mode
 * icon + theme name and opens a CURATED appearance menu (ADR-0007 §2): the Aurora
 * theme + light/dark/system mode via the EXISTING `ModeToggle` / `useThemeMode`.
 * The deferred 7-theme picker, accent, and density axes are intentionally absent.
 */
import { ModeToggle, Icon, useThemeMode, THEME_NAME, type IconName } from '@/ui';
import type { ThemeMode } from '@/ui';
import { useDisclosure } from './useDisclosure';

const MODE_ICON: Record<ThemeMode, IconName> = {
  light: 'sun',
  dark: 'moon',
  system: 'monitor',
};

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function AppearanceMenu() {
  const { mode } = useThemeMode();
  const { open, toggle, triggerRef, menuRef } = useDisclosure();

  return (
    <div style={{ position: 'relative' }}>
      <button
        ref={triggerRef}
        type="button"
        className="lc-pillbtn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Appearance — ${titleCase(THEME_NAME)} theme, ${mode} mode`}
        onClick={toggle}
      >
        <Icon name={MODE_ICON[mode]} />
        <span>{titleCase(THEME_NAME)}</span>
        <Icon name="chevron-down" />
      </button>

      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Appearance"
          className="lc-menu"
          style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0 }}
        >
          <div className="lc-menu__head">
            <div className="lc-menu__name">Appearance</div>
            <div className="lc-menu__meta">
              {titleCase(THEME_NAME)} theme · applies instantly, saved on this device.
            </div>
          </div>
          <div className="lc-menu__sep" />
          <div className="lc-menu__label">Mode</div>
          <div style={{ padding: '2px 6px 6px' }}>
            <ModeToggle />
          </div>
        </div>
      ) : null}
    </div>
  );
}
