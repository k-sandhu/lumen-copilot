/**
 * Displays the knowledge modes governing the next answer. Chat supplies the
 * server-authoritative pinned scope and makes this group read-only (#581).
 * Active modes remain visible via aria-pressed; excluded modes and Model have
 * accessible explanations. Reusable callers with an onChange handler can edit
 * available modes, while omitting the handler always disables the group.
 */
import { useId } from 'react';
import { Icon } from '@/ui';
import type { KnowledgeMode } from '@/api';
import {
  MODE_CONFIG,
  MODE_ORDER,
  resolveAvailability,
  type ModeAvailability,
} from '../model/knowledgeModes';

export type { ModeAvailability };

export interface KnowledgeModeControlProps {
  /** The currently-active modes for the next turn. */
  value: readonly KnowledgeMode[];
  /** Reports toggles up. Omit (or set `readOnly`) for a display-only scope. */
  onChange?: (next: KnowledgeMode[]) => void;
  /**
   * Per-mode availability. A mode absent from the map is available by default,
   * EXCEPT web, which is unavailable unless it is explicitly present-and-available
   * (governed / off by default — ADR-0014), and model, which is never available
   * (#581). This makes "web off" the safe default.
   */
  availability?: Partial<Record<KnowledgeMode, ModeAvailability>>;
  /** Disable the whole group (e.g. while an answer is streaming). */
  disabled?: boolean;
  /**
   * Display-only (#581): the scope is server-authoritative and not adjustable
   * in chat (`adjustable: false`), so every chip renders disabled —
   * `aria-pressed` shows which modes are actually in use, and no control
   * pretends a click would change anything.
   */
  readOnly?: boolean;
}

export function KnowledgeModeControl({
  value,
  onChange,
  availability,
  disabled = false,
  readOnly = false,
}: KnowledgeModeControlProps) {
  const active = new Set(value);
  const reasonId = useId();

  const toggle = (mode: KnowledgeMode) => {
    if (readOnly || !onChange) return;
    const next = new Set(active);
    if (next.has(mode)) next.delete(mode);
    else next.add(mode);
    // Preserve the canonical order so the reported set is stable.
    onChange(MODE_ORDER.filter((m) => next.has(m)));
  };

  return (
    <div role="group" aria-label="Knowledge modes" className="flex flex-wrap items-center gap-2">
      {MODE_ORDER.map((mode) => {
        const cfg = MODE_CONFIG[mode];
        const avail = resolveAvailability(mode, availability);
        const isDisabled = disabled || readOnly || !onChange || !avail.available;
        const on = active.has(mode) && avail.available;
        // A disabled mode gets a describedby pointing at its reason so screen
        // readers announce WHY it can't be turned on (not a silent no-op).
        const showReason = !avail.available && avail.reason;
        const describedBy = showReason ? `${reasonId}-${mode}` : undefined;
        return (
          <span key={mode} className="inline-flex">
            <button
              type="button"
              aria-pressed={on}
              disabled={isDisabled}
              title={avail.available ? cfg.hint : avail.reason}
              onClick={() => toggle(mode)}
              className="lc-mode-chip"
              {...(describedBy ? { 'aria-describedby': describedBy } : {})}
            >
              <Icon name={cfg.icon} />
              {cfg.label}
              {mode === 'model' && (
                <span aria-hidden="true" className="text-xs">
                  not used
                </span>
              )}
            </button>
            {showReason ? (
              <span id={describedBy} hidden>
                {avail.reason}
              </span>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}
