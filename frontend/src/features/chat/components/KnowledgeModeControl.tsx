/**
 * KnowledgeModeControl (#221, epic E3-12 "active knowledge modes visible/
 * changeable") — the composer control that surfaces WHICH knowledge modes the
 * next answer may draw on: company / uploaded / web / model. It reflects the
 * active assistant's `knowledgeScope.modes` when a session was started from an
 * assistant, and allows a per-chat override where the mode is available.
 *
 * These are the four wire modes (`KnowledgeMode`, contracts/openapi.yaml
 * §assistants). Each is a toggle button in a labelled group (multi-select — an
 * answer can draw on more than one class at once), keyboard-accessible via
 * `aria-pressed`.
 *
 * WEB is governed (ADR-0014 / #219): the `web_search` tool is off by default and
 * gated at the tenant/assistant level. When web is NOT enabled for this session,
 * the web toggle renders **disabled with a clear reason** (a tooltip + an
 * accessible description) — never a silent no-op that pretends to work.
 *
 * Presentational: the composer owns the selected-mode set (local UI state) and
 * the availability facts; this renders them and reports toggles up. The config +
 * availability logic live in ../model/knowledgeModes so this file exports only a
 * component (react-refresh) and the logic stays unit-testable.
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
  onChange: (next: KnowledgeMode[]) => void;
  /**
   * Per-mode availability. A mode absent from the map is available by default,
   * EXCEPT web, which is unavailable unless it is explicitly present-and-available
   * (governed / off by default — ADR-0014). This makes "web off" the safe default.
   */
  availability?: Partial<Record<KnowledgeMode, ModeAvailability>>;
  /** Disable the whole group (e.g. while an answer is streaming). */
  disabled?: boolean;
}

export function KnowledgeModeControl({
  value,
  onChange,
  availability,
  disabled = false,
}: KnowledgeModeControlProps) {
  const active = new Set(value);
  const reasonId = useId();

  const toggle = (mode: KnowledgeMode) => {
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
        const isDisabled = disabled || !avail.available;
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
