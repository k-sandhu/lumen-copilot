/**
 * SearchTypeahead (spec 0005, epic #144) — the search box as an accessible
 * combobox. As the caller types it debounces and calls `GET /search/suggest`
 * (permission-trimmed server suggestions: completions from their recents/saved +
 * permitted document titles); with an empty box it offers their **Recent** queries
 * and **Saved** searches. Keyboard-navigable (Arrow/Enter/Escape,
 * `aria-activedescendant`); choosing an option runs the query (and, for a saved
 * search, applies its filters). It degrades to plain submit-to-search if `suggest`
 * errors — typing and Enter always work.
 */
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';
import type { SavedSearch } from '@/api';
import {
  useClearRecentSearches,
  useDeleteSavedSearch,
  useRecentSearches,
  useSavedSearches,
  useSuggest,
} from '../model/queries';

export interface SearchTypeaheadProps {
  value: string;
  onChange: (next: string) => void;
  /** Run a plain query (a typed query / completion / document / recent). */
  onRunQuery: (q: string) => void;
  /** Apply a saved search — its query AND its filters. */
  onApplySaved: (saved: SavedSearch) => void;
  busy?: boolean;
}

/** One flat, keyboard-navigable option in the dropdown. */
interface Option {
  key: string;
  group: 'run' | 'suggestion' | 'recent' | 'saved';
  label: string;
  icon: 'search' | 'clock' | 'file-text' | 'list';
  run: () => void;
  /** Saved searches get an inline delete; recents a group clear. */
  savedId?: string;
}

const DEBOUNCE_MS = 180;

export function SearchTypeahead({
  value,
  onChange,
  onRunQuery,
  onApplySaved,
  busy = false,
}: SearchTypeaheadProps) {
  const inputId = useId();
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [debounced, setDebounced] = useState(value);
  const blurTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const trimmed = value.trim();

  // Debounce the box → the suggest query key (one request per ~typing pause).
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [value]);

  const suggest = useSuggest(open ? debounced : '');
  const recent = useRecentSearches();
  const saved = useSavedSearches();
  const clearRecent = useClearRecentSearches();
  const removeSaved = useDeleteSavedSearch();

  const options = useMemo<Option[]>(() => {
    const savedItems = saved.data?.items ?? [];
    const out: Option[] = [];
    if (trimmed.length === 0) {
      for (const r of recent.data?.items ?? []) {
        out.push({
          key: `recent:${r.query}`,
          group: 'recent',
          label: r.query,
          icon: 'clock',
          run: () => onRunQuery(r.query),
        });
      }
      for (const s of savedItems) {
        out.push({
          key: `saved:${s.id}`,
          group: 'saved',
          label: s.name,
          icon: 'list',
          savedId: s.id,
          run: () => onApplySaved(s),
        });
      }
      return out;
    }
    // Typing: a lead "Search for …" then the permission-trimmed server suggestions.
    out.push({
      key: 'run',
      group: 'run',
      label: `Search for “${trimmed}”`,
      icon: 'search',
      run: () => onRunQuery(trimmed),
    });
    for (const s of suggest.data?.suggestions ?? []) {
      if (s.kind === 'saved_search') {
        const match = savedItems.find((x) => x.id === s.saved_search_id);
        out.push({
          key: `sug-saved:${s.saved_search_id ?? s.text}`,
          group: 'saved',
          label: s.text,
          icon: 'list',
          ...(match ? { savedId: match.id } : {}),
          run: match ? () => onApplySaved(match) : () => onRunQuery(s.text),
        });
      } else {
        out.push({
          key: `sug:${s.kind}:${s.document_id ?? s.text}`,
          group: 'suggestion',
          label: s.text,
          icon: s.kind === 'document' ? 'file-text' : 'search',
          run: () => onRunQuery(s.text),
        });
      }
    }
    return out;
  }, [trimmed, recent.data, saved.data, suggest.data, onRunQuery, onApplySaved]);

  // Keep the active index in range as the option set changes.
  useEffect(() => {
    setActive((a) => (a >= options.length ? options.length - 1 : a));
  }, [options.length]);

  const hasDropdown = open && options.length > 0;

  function choose(opt: Option) {
    opt.run();
    setOpen(false);
    setActive(-1);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, options.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, -1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (hasDropdown && active >= 0 && options[active]) choose(options[active]);
      else if (trimmed.length > 0) {
        onRunQuery(trimmed);
        setOpen(false);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
      setActive(-1);
    }
  }

  const activeId = hasDropdown && active >= 0 ? `${listId}-opt-${active}` : undefined;

  return (
    <div className="relative">
      <label htmlFor={inputId} className="sr-only">
        Search your sources
      </label>
      <div className="relative flex items-center">
        <span className="pointer-events-none absolute left-3 text-foreground-muted" aria-hidden="true">
          <Icon name="search" />
        </span>
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-expanded={hasDropdown}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeId}
          aria-busy={busy}
          autoComplete="off"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onKeyDown={onKeyDown}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            blurTimer.current = setTimeout(() => setOpen(false), 120);
          }}
          placeholder="Search across your permitted sources…"
          aria-label="Search your sources"
          className="w-full rounded-md border border-border bg-surface py-2 pl-10 pr-3 text-sm text-foreground placeholder:text-foreground-muted focus:outline-none focus:ring-2 focus:ring-accent"
        />
      </div>

      {hasDropdown ? (
        <ul
          id={listId}
          role="listbox"
          aria-label="Search suggestions"
          className="absolute z-30 mt-1 max-h-80 w-full overflow-auto rounded-md border border-border bg-surface py-1 shadow-lg"
          // Keep focus on the input while the user mouses over options.
          onMouseDown={(e) => e.preventDefault()}
        >
          {options.map((opt, i) => {
            const first = i === 0 || options[i - 1]?.group !== opt.group;
            return (
              <li key={opt.key}>
                {first && opt.group !== 'run' ? (
                  <div className="flex items-center justify-between px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-foreground-muted">
                    <span>
                      {opt.group === 'recent'
                        ? 'Recent'
                        : opt.group === 'saved'
                          ? 'Saved searches'
                          : 'Suggestions'}
                    </span>
                    {opt.group === 'recent' ? (
                      <button
                        type="button"
                        className="text-[11px] font-medium normal-case text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                        onClick={() => clearRecent.mutate()}
                      >
                        Clear
                      </button>
                    ) : null}
                  </div>
                ) : null}
                <div
                  id={`${listId}-opt-${i}`}
                  role="option"
                  aria-selected={i === active}
                  onClick={() => choose(opt)}
                  onMouseEnter={() => setActive(i)}
                  className={cn(
                    'flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm text-foreground',
                    i === active ? 'bg-accent/15 text-accent' : 'hover:bg-surface-muted',
                  )}
                >
                  <Icon name={opt.icon} className="h-3.5 w-3.5 shrink-0 text-foreground-muted" />
                  <span className="min-w-0 flex-1 truncate">{opt.label}</span>
                  {opt.savedId ? (
                    <button
                      type="button"
                      aria-label={`Delete saved search ${opt.label}`}
                      className="shrink-0 rounded p-0.5 text-foreground-muted hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeSaved.mutate(opt.savedId as string);
                      }}
                    >
                      <Icon name="x" className="h-3.5 w-3.5" />
                    </button>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
