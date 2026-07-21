/**
 * KnowledgeScopePicker (#212) — the assistant's retrieval scope (ADR-0011 §1/§2):
 * which knowledge MODES it may draw from (company / uploaded / web / model), and
 * an optional narrowing to specific COLLECTIONS and SOURCES. A narrowing set only
 * intersects the per-user permission predicate inside retrieval/ — it can never
 * widen access — so an empty list means "no narrowing", not "nothing".
 *
 * Each async list (collections, sources) implements its own loading / error /
 * empty state so the picker is never a blank pane while data resolves.
 *
 * A11y: mode toggles + list checkboxes are native inputs inside labelled
 * fieldsets; long collection/source names truncate rather than break layout.
 */
import { useId } from 'react';
import { ApiError } from '@/api';
import type { Collection, KnowledgeMode, KnowledgeScope, Source } from '@/api';
import { useCollections, useSources } from '../model/queries';
import { KNOWLEDGE_MODE_OPTIONS } from '../model/presentation';

interface KnowledgeScopePickerProps {
  value: KnowledgeScope;
  onChange: (next: KnowledgeScope) => void;
  disabled?: boolean;
}

export function KnowledgeScopePicker({
  value,
  onChange,
  disabled = false,
}: KnowledgeScopePickerProps) {
  const collections = useCollections();
  const sources = useSources();

  const modes = new Set(value.modes ?? []);
  const collectionIds = new Set(value.collectionIds ?? []);
  const sourceIds = new Set(value.sourceIds ?? []);

  const toggleMode = (mode: KnowledgeMode) => {
    const next = new Set(modes);
    if (next.has(mode)) next.delete(mode);
    else next.add(mode);
    onChange({ ...value, modes: [...next] });
  };

  const toggleCollection = (id: string) => {
    const next = new Set(collectionIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ ...value, collectionIds: [...next] });
  };

  const toggleSource = (id: string) => {
    const next = new Set(sourceIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ ...value, sourceIds: [...next] });
  };

  return (
    <div className="space-y-4">
      <fieldset disabled={disabled} className="min-w-0">
        <legend className="text-sm font-medium">Knowledge modes</legend>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Which classes of knowledge this assistant may draw from.
        </p>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2" role="group">
          {KNOWLEDGE_MODE_OPTIONS.map((opt) => (
            <ModeToggle
              key={opt.value}
              label={opt.label}
              hint={opt.hint}
              checked={modes.has(opt.value)}
              onToggle={() => toggleMode(opt.value)}
            />
          ))}
        </div>
      </fieldset>

      <ScopeList
        legend="Collections"
        emptyLabel="No collections yet — the scope isn’t narrowed by collection."
        errorLabel="Couldn’t load collections."
        isPending={collections.isPending}
        isError={collections.isError}
        error={collections.error}
        onRetry={() => void collections.refetch()}
        items={collections.data?.items ?? []}
        selected={collectionIds}
        onToggle={toggleCollection}
        renderLabel={(c: Collection) => c.name}
        disabled={disabled}
      />

      <ScopeList
        legend="Sources"
        emptyLabel="No connected sources — the scope isn’t narrowed by source."
        errorLabel="Couldn’t load sources."
        isPending={sources.isPending}
        isError={sources.isError}
        error={sources.error}
        onRetry={() => void sources.refetch()}
        items={sources.data?.items ?? []}
        selected={sourceIds}
        onToggle={toggleSource}
        renderLabel={(s: Source) =>
          // `Source` is a type-discriminated union since the managed gdrive
          // connector (ADR-0019): only the web branch carries a URL.
          s.type === 'web' ? s.config.url : 'Google Drive'
        }
        disabled={disabled}
      />
    </div>
  );
}

function ModeToggle({
  label,
  hint,
  checked,
  onToggle,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onToggle: () => void;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2"
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="mt-0.5 h-4 w-4 shrink-0 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <span className="min-w-0">
        <span className="block text-sm">{label}</span>
        <span className="block text-xs text-foreground-muted">{hint}</span>
      </span>
    </label>
  );
}

interface ScopeListProps<T extends { id: string }> {
  legend: string;
  emptyLabel: string;
  errorLabel: string;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
  items: T[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  renderLabel: (item: T) => string;
  disabled: boolean;
}

function ScopeList<T extends { id: string }>({
  legend,
  emptyLabel,
  errorLabel,
  isPending,
  isError,
  error,
  onRetry,
  items,
  selected,
  onToggle,
  renderLabel,
  disabled,
}: ScopeListProps<T>) {
  const groupId = useId();
  return (
    <fieldset disabled={disabled} className="min-w-0">
      <legend className="text-sm font-medium">{legend}</legend>
      {isPending ? (
        <div
          role="status"
          className="mt-2 space-y-2"
          aria-label={`Loading ${legend.toLowerCase()}`}
        >
          {[0, 1].map((i) => (
            <div key={i} className="lc-skeleton" style={{ width: '70%' }} aria-hidden="true" />
          ))}
        </div>
      ) : isError ? (
        <div
          role="alert"
          className="mt-2 rounded-md border border-danger/40 bg-danger/10 p-2 text-xs"
        >
          <p className="text-danger">
            {error instanceof ApiError && error.status === 401
              ? 'Your session expired — sign in again.'
              : errorLabel}
          </p>
          {!(error instanceof ApiError && error.status === 401) ? (
            <button
              type="button"
              onClick={onRetry}
              className="mt-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Retry
            </button>
          ) : null}
        </div>
      ) : items.length === 0 ? (
        <p className="mt-2 text-xs text-foreground-muted">{emptyLabel}</p>
      ) : (
        <ul className="mt-2 max-h-40 space-y-1.5 overflow-y-auto" aria-label={legend}>
          {items.map((item) => {
            const id = `${groupId}-${item.id}`;
            const label = renderLabel(item);
            return (
              <li key={item.id} className="min-w-0">
                <label htmlFor={id} className="flex cursor-pointer items-center gap-2">
                  <input
                    id={id}
                    type="checkbox"
                    checked={selected.has(item.id)}
                    onChange={() => onToggle(item.id)}
                    className="h-4 w-4 shrink-0 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  />
                  <span className="truncate text-sm" title={label}>
                    {label}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </fieldset>
  );
}
