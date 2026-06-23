/**
 * CommandPalette — fast navigation/actions via ⌘/Ctrl-K (DESIGN.md §6). One of
 * the three appearance-system pieces shipping in v1 (ADR-0007 §2).
 *
 * A11y: a role="dialog" combobox/listbox pattern — the input owns
 * aria-activedescendant pointing at the highlighted option; ↑/↓ move the active
 * item, Enter runs it, Escape closes. Focus moves to the input on open and
 * returns to the opener on close. Motion is CSS-only (collapses under
 * prefers-reduced-motion).
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/cn';
import { Icon, type IconName } from './Icon';

export interface CommandItem {
  id: string;
  label: string;
  /** Optional secondary search terms / aliases. */
  keywords?: string;
  icon?: IconName;
  /** Right-aligned hint, e.g. a shortcut or section. */
  hint?: string;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  items: CommandItem[];
  placeholder?: string;
}

export function CommandPalette({ open, onClose, items, placeholder }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const lastFocused = useRef<HTMLElement | null>(null);
  const listId = useId();
  const optionId = (i: number): string => `${listId}-opt-${i}`;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (it) =>
        it.label.toLowerCase().includes(q) || (it.keywords?.toLowerCase().includes(q) ?? false),
    );
  }, [items, query]);

  // Reset query/selection and manage focus across open transitions. The input
  // is mounted in the same commit (the early `return null` is gated on `open`),
  // so it is focusable synchronously here.
  useEffect(() => {
    if (open) {
      lastFocused.current = (document.activeElement as HTMLElement) ?? null;
      setQuery('');
      setActiveIndex(0);
      inputRef.current?.focus();
      return undefined;
    }
    lastFocused.current?.focus?.();
    return undefined;
  }, [open]);

  // Keep the active index in range as the filtered set changes.
  useEffect(() => {
    setActiveIndex((i) => Math.min(i, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  if (!open) return null;

  const run = (item: CommandItem | undefined): void => {
    if (!item) return;
    item.run();
    onClose();
  };

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => (filtered.length ? (i + 1) % filtered.length : 0));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => (filtered.length ? (i - 1 + filtered.length) % filtered.length : 0));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      run(filtered[activeIndex]);
    }
  };

  return (
    <div className="lc-cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
      <button
        type="button"
        className="lc-cmdk__backdrop"
        aria-label="Close command palette"
        tabIndex={-1}
        onClick={onClose}
      />
      <div className="lc-cmdk__card">
        <div className="lc-cmdk__input">
          <Icon name="search" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            placeholder={placeholder ?? 'Search commands…'}
            aria-label="Search commands"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={filtered.length ? optionId(activeIndex) : undefined}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onKeyDown}
          />
          <span className="lc-kbd">Esc</span>
        </div>
        {filtered.length === 0 ? (
          <div className="lc-cmdk__empty">No commands match “{query}”.</div>
        ) : (
          <ul className="lc-cmdk__list" id={listId} role="listbox" aria-label="Commands">
            {filtered.map((item, i) => (
              <li key={item.id} role="presentation">
                <button
                  type="button"
                  id={optionId(i)}
                  role="option"
                  aria-selected={i === activeIndex}
                  className={cn('lc-cmdk__item')}
                  onMouseMove={() => setActiveIndex(i)}
                  onClick={() => run(item)}
                  tabIndex={-1}
                >
                  {item.icon ? <Icon name={item.icon} /> : null}
                  <span>{item.label}</span>
                  {item.hint ? <span className="lc-cmdk__hint">{item.hint}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
