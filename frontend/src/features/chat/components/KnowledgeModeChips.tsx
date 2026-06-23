/**
 * Knowledge-mode chips (#89, wireframe chat composer; DESIGN.md §1 "grounded").
 * A small, keyboard-accessible single-select group that surfaces WHAT the next
 * answer is grounded on — the trust signal that "Lumen answers from your
 * permitted sources". This is presentational scope state (Zustand-free local UI
 * state lives in the composer); it maps onto behavior the contract ALREADY
 * supports and invents none:
 *
 *   - "Company sources" → retrieve across all permitted docs (omit
 *     `collection_ids`) — the existing default behavior.
 *   - "Selected sources" → restrict retrieval to chosen collections
 *     (`collection_ids`). Disabled until a collection scope is supplied, so we
 *     never imply a capability that isn't wired.
 *
 * Modes the contract has no backing for (web, model-only knowledge) are NOT
 * offered — no invented product behavior (AGENTS.md §8).
 */
import { Icon, type IconName } from '@/ui';
import { cn } from '@/lib/cn';

export type KnowledgeMode = 'company' | 'selected';

interface ModeConfig {
  label: string;
  icon: IconName;
  hint: string;
}

const MODES: Record<KnowledgeMode, ModeConfig> = {
  company: {
    label: 'Company sources',
    icon: 'database',
    hint: 'Answer from all sources you have permission to see',
  },
  selected: {
    label: 'Selected sources',
    icon: 'list',
    hint: 'Restrict the answer to specific collections',
  },
};

const ORDER: KnowledgeMode[] = ['company', 'selected'];

export interface KnowledgeModeChipsProps {
  value: KnowledgeMode;
  onChange: (mode: KnowledgeMode) => void;
  /** Modes with no backing scope are disabled, so we never imply a fake feature. */
  selectedAvailable?: boolean;
  disabled?: boolean;
}

export function KnowledgeModeChips({
  value,
  onChange,
  selectedAvailable = false,
  disabled = false,
}: KnowledgeModeChipsProps) {
  return (
    <div
      role="group"
      aria-label="Knowledge mode"
      className="flex flex-wrap items-center gap-1.5"
    >
      {ORDER.map((mode) => {
        const cfg = MODES[mode];
        const unavailable = mode === 'selected' && !selectedAvailable;
        const isDisabled = disabled || unavailable;
        const active = value === mode;
        return (
          <button
            key={mode}
            type="button"
            aria-pressed={active}
            disabled={isDisabled}
            title={unavailable ? 'Pick a collection to scope sources' : cfg.hint}
            onClick={() => onChange(mode)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              active
                ? 'border-accent/40 bg-accent/15 text-accent'
                : 'border-border text-foreground-muted hover:bg-surface-muted',
              isDisabled && 'cursor-not-allowed opacity-50',
            )}
          >
            <Icon name={cfg.icon} className="h-3.5 w-3.5" />
            {cfg.label}
          </button>
        );
      })}
    </div>
  );
}
