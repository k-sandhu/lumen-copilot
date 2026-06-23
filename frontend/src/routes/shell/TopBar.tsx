/**
 * Top bar (issue #110) — the pinned chrome row: a rail-toggle button, the omni
 * bar (opens the command palette), and the trailing actions (appearance, tenant
 * pill, notifications, account). Matches docs/wireframes (buildChrome) on
 * production tokens. Each control is its own component so the shell composes them.
 */
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/Tooltip';
import { Icon } from '@/ui';
import { OmniBar } from './OmniBar';
import { AppearanceMenu } from './AppearanceMenu';
import { TenantPill } from './TenantPill';
import { AccountMenu } from './AccountMenu';

interface TopBarProps {
  onOpenPalette: () => void;
  onToggleRail: () => void;
}

export function TopBar({ onOpenPalette, onToggleRail }: TopBarProps) {
  return (
    <header className="lc-shell__topbar">
      <button
        type="button"
        className="lc-iconbtn--ghost"
        onClick={onToggleRail}
        aria-label="Toggle navigation rail"
      >
        <Icon name="list" />
      </button>

      <OmniBar onOpen={onOpenPalette} />

      <div className="lc-shell__spacer" />

      <div className="lc-topbar__actions">
        <AppearanceMenu />
        <TenantPill />
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" className="lc-iconbtn--ghost" aria-label="Notifications">
              <Icon name="bell" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Notifications</TooltipContent>
        </Tooltip>
        <AccountMenu />
      </div>
    </header>
  );
}
