/**
 * Appearance control (issues #110, #134) — the top-bar trigger shows the current
 * mode icon + theme name and opens the full Appearance & preferences panel
 * (theme / mode / accent / density). The appearance api is owned here and passed
 * into the panel so the trigger label stays in lock-step with the panel's edits.
 */
import { Icon, useAppearance, THEMES, type IconName } from '@/ui';
import type { ThemeMode } from '@/ui';
import { DefaultModelSetting } from '@/features/preferences/components/DefaultModelSetting';
import { useDisclosure } from './useDisclosure';
import { AppearancePanel } from './AppearancePanel';

const MODE_ICON: Record<ThemeMode, IconName> = {
  light: 'sun',
  dark: 'moon',
  system: 'monitor',
};

export function AppearanceMenu() {
  const appearance = useAppearance();
  const { open, toggle, triggerRef, menuRef } = useDisclosure();

  const themeLabel = THEMES.find((t) => t.id === appearance.theme)?.label ?? 'Theme';

  return (
    <div style={{ position: 'relative' }}>
      <button
        ref={triggerRef}
        type="button"
        className="lc-pillbtn"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Appearance — ${themeLabel} theme, ${appearance.mode} mode`}
        onClick={toggle}
      >
        <Icon name={MODE_ICON[appearance.mode]} />
        <span>{themeLabel}</span>
        <Icon name="chevron-down" />
      </button>

      {open ? (
        // A preferences popover, not a menu: its content is grouped toggle-button
        // controls (theme/mode/accent/density), not menuitems — so it exposes
        // role="dialog" (labelled) rather than role="menu" (#134 review).
        <div
          ref={menuRef}
          role="dialog"
          aria-label="Appearance & preferences"
          className="lc-menu"
          style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0, minWidth: 'unset' }}
        >
          <div className="lc-menu__head">
            <div className="lc-menu__name">Appearance &amp; preferences</div>
            <div className="lc-menu__meta">
              Appearance saved on this device · default model synced to your account.
            </div>
          </div>
          <div className="lc-menu__sep" />
          <AppearancePanel appearance={appearance}>
            <DefaultModelSetting />
          </AppearancePanel>
        </div>
      ) : null}
    </div>
  );
}
