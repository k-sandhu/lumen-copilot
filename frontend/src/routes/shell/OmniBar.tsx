/**
 * Omni bar (issue #110) — the top-bar "Search or ask across your workspace… ⌘K"
 * affordance that OPENS the existing command palette (`ui/CommandPalette` +
 * `useCommandPalette`). It is a button (not an input): clicking, Enter, or the
 * global ⌘/Ctrl-K shortcut all open the palette, which owns the actual querying.
 */
import { Icon } from '@/ui';

interface OmniBarProps {
  onOpen: () => void;
}

export function OmniBar({ onOpen }: OmniBarProps) {
  return (
    <button
      type="button"
      className="lc-omni"
      onClick={onOpen}
      aria-keyshortcuts="Control+K Meta+K"
      aria-label="Search or ask across your workspace — opens the command palette"
    >
      <Icon name="search" />
      <span className="lc-omni__label">Search or ask across your workspace…</span>
      <span className="lc-kbd" aria-hidden="true">
        ⌘K
      </span>
    </button>
  );
}
