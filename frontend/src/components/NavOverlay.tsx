import { useId, useRef, useState } from 'react';
import type { FocusEvent, KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';

export interface NavOverlayItem {
  to: string;
  label: string;
  icon?: string;
}

interface NavOverlayProps {
  items: NavOverlayItem[];
  /** Accessible name for the trigger + popover. */
  label?: string;
}

/**
 * Floating hover/focus overlay that links to the standalone developer pages
 * (docs viewer, features catalog) from the main app — the "overlay / hover links"
 * the user asked for. Disclosure pattern: a small fixed trigger that reveals the
 * links on hover, and on click/Enter for touch + keyboard. Esc closes and returns
 * focus to the trigger; tabbing out closes it. Transitions defer to the global
 * prefers-reduced-motion rule.
 */
export function NavOverlay({ items, label = 'Developer pages' }: NavOverlayProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  function handleMouseLeave() {
    // Don't snap shut if a child still holds keyboard focus.
    if (!containerRef.current?.contains(document.activeElement)) setOpen(false);
  }

  function handleBlur(event: FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && open) {
      setOpen(false);
      triggerRef.current?.focus();
    }
  }

  return (
    <div
      ref={containerRef}
      className="fixed bottom-4 right-4 z-50 flex flex-col-reverse items-end gap-2 print:hidden"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={handleMouseLeave}
      onBlur={handleBlur}
      onKeyDown={handleKeyDown}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={label}
        // Open on click/Enter (keyboard + touch); hover opens for mouse. Closing
        // is via Esc, blur, or mouse-leave — so a hover-then-click never collapses it.
        onClick={() => setOpen(true)}
        className="rounded-full border border-border bg-surface px-4 py-2 text-sm font-medium shadow-lg hover:bg-surface-muted"
      >
        <span aria-hidden="true">🧭 </span>Pages
      </button>

      {open ? (
        <nav
          id={panelId}
          aria-label={label}
          className="flex w-52 flex-col gap-1 rounded-lg border border-border bg-surface p-2 shadow-xl"
        >
          {items.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="flex items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-surface-muted"
            >
              {item.icon ? <span aria-hidden="true">{item.icon}</span> : null}
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>
      ) : null}
    </div>
  );
}
