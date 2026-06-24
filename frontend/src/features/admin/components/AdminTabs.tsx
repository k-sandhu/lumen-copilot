/**
 * AdminTabs — the segmented tab bar for the read-only admin console (#122,
 * admin.html). A purely navigational control: it switches which governance panel
 * is visible (Members & roles / Model governance / Approvals & risk / Data
 * minimization). It mutates NO server state (ADR-0007 §4) — selecting a tab is
 * local UI state only.
 *
 * A11y: a proper WAI-ARIA tablist. Each tab is a `role="tab"` button carrying
 * `aria-selected` and `aria-controls`; arrow keys move between tabs (roving
 * tabindex) and the active tab is the only one in the tab order. Panels wired by
 * the caller carry `role="tabpanel"` + the matching `aria-labelledby`.
 */
import { useCallback, useId, useRef } from 'react';
import { Icon, type IconName } from '@/ui';
import { cn } from '@/lib/cn';
import { adminTabIds } from './tabIds';

export interface AdminTab {
  id: string;
  label: string;
  icon: IconName;
}

interface AdminTabsProps {
  tabs: AdminTab[];
  /** The currently selected tab id. */
  value: string;
  onChange: (id: string) => void;
  /** Stable id prefix so tab/panel `aria-*` wiring is deterministic. */
  idPrefix: string;
}

export function AdminTabs({ tabs, value, onChange, idPrefix }: AdminTabsProps) {
  // A fallback prefix if a caller forgets to pass one (keeps ids unique per mount).
  const fallback = useId();
  const prefix = idPrefix || fallback;
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const focusTab = useCallback(
    (index: number) => {
      const next = (index + tabs.length) % tabs.length;
      const target = tabs[next];
      if (!target) return;
      onChange(target.id);
      refs.current[next]?.focus();
    },
    [onChange, tabs],
  );

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent, index: number) => {
      switch (event.key) {
        case 'ArrowRight':
        case 'ArrowDown':
          event.preventDefault();
          focusTab(index + 1);
          break;
        case 'ArrowLeft':
        case 'ArrowUp':
          event.preventDefault();
          focusTab(index - 1);
          break;
        case 'Home':
          event.preventDefault();
          focusTab(0);
          break;
        case 'End':
          event.preventDefault();
          focusTab(tabs.length - 1);
          break;
        default:
          break;
      }
    },
    [focusTab, tabs.length],
  );

  return (
    <div
      role="tablist"
      aria-label="Admin sections"
      aria-orientation="horizontal"
      className="inline-flex flex-wrap gap-1 rounded-lg border border-border bg-surface-muted p-1"
    >
      {tabs.map((tab, index) => {
        const selected = tab.id === value;
        const ids = adminTabIds(prefix, tab.id);
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[index] = el;
            }}
            type="button"
            role="tab"
            id={ids.tab}
            aria-selected={selected}
            aria-controls={ids.panel}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={cn(
              'inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              selected
                ? 'bg-surface text-foreground shadow-sm'
                : 'text-foreground-muted hover:text-foreground',
            )}
          >
            <Icon name={tab.icon} aria-hidden="true" />
            <span>{tab.label}</span>
          </button>
        );
      })}
    </div>
  );
}
