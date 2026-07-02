/**
 * CadenceInput (#237, AC-4) — the cron/cadence builder for the schedule form. The
 * user toggles between a friendly structured recurrence (every hour/day/week/month
 * at a local time, with a weekday/day-of-month selector) and a raw 5-field cron
 * expression. Both are validated client-side with FRIENDLY messages before the
 * round-trip; the server stays authoritative (a bad cron → 422 `invalid_cron`).
 *
 * Presentational: it takes the draft + onChange + an optional error and renders.
 * A11y: a radiogroup for the mode, labelled inputs, an aria-describedby error
 * wired to the active input.
 */
import { useId } from 'react';
import { Icon } from '@/ui';
import {
  CADENCE_UNITS,
  WEEKDAYS,
  validateCron,
  validateTimeOfDay,
  type CadenceDraft,
} from '../model/cadence';

interface CadenceInputProps {
  value: CadenceDraft;
  onChange: (draft: CadenceDraft) => void;
  disabled?: boolean;
  /** A submit-time error surfaced by the parent (e.g. a 422 from the server). */
  error?: string | null;
}

const UNIT_LABEL: Record<(typeof CADENCE_UNITS)[number], string> = {
  hour: 'Hour',
  day: 'Day',
  week: 'Week',
  month: 'Month',
};

export function CadenceInput({ value, onChange, disabled, error }: CadenceInputProps) {
  const cronId = useId();
  const atId = useId();
  const set = <K extends keyof CadenceDraft>(key: K, v: CadenceDraft[K]) =>
    onChange({ ...value, [key]: v });

  // Live (as-you-type) validation for the active mode, so the message appears
  // before submit; the parent error (a server 422) takes precedence when present.
  const liveError =
    value.mode === 'cron' ? validateCron(value.cron) : validateTimeOfDay(value.at);
  const shownError = error ?? liveError;
  const errId = `${cronId}-error`;

  return (
    <fieldset className="space-y-3" disabled={disabled}>
      <legend className="mb-1 block text-sm font-medium">Cadence</legend>

      <div role="radiogroup" aria-label="Cadence type" className="flex gap-2">
        <ModeChip
          active={value.mode === 'structured'}
          label="Simple"
          onClick={() => set('mode', 'structured')}
          disabled={disabled}
        />
        <ModeChip
          active={value.mode === 'cron'}
          label="Cron expression"
          onClick={() => set('mode', 'cron')}
          disabled={disabled}
        />
      </div>

      {value.mode === 'structured' ? (
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-foreground-muted">
            <span>Every</span>
            <select
              aria-label="Recurrence period"
              value={value.every}
              onChange={(e) => set('every', e.target.value as CadenceDraft['every'])}
              className="w-32 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
            >
              {CADENCE_UNITS.map((u) => (
                <option key={u} value={u}>
                  {UNIT_LABEL[u]}
                </option>
              ))}
            </select>
          </label>

          {value.every === 'week' ? (
            <label className="flex flex-col gap-1 text-xs text-foreground-muted">
              <span>On</span>
              <select
                aria-label="Day of week"
                value={value.dayOfWeek}
                onChange={(e) => set('dayOfWeek', Number(e.target.value))}
                className="w-36 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
              >
                {WEEKDAYS.map((day, i) => (
                  <option key={day} value={i}>
                    {day}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          {value.every === 'month' ? (
            <label className="flex flex-col gap-1 text-xs text-foreground-muted">
              <span>Day of month</span>
              <input
                type="number"
                aria-label="Day of month"
                min={1}
                max={31}
                value={value.dayOfMonth}
                onChange={(e) => set('dayOfMonth', Number(e.target.value))}
                className="w-24 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
              />
            </label>
          ) : null}

          {value.every !== 'hour' ? (
            <label htmlFor={atId} className="flex flex-col gap-1 text-xs text-foreground-muted">
              <span>At (local)</span>
              <input
                id={atId}
                type="time"
                value={value.at}
                aria-invalid={shownError ? true : undefined}
                aria-describedby={shownError ? errId : undefined}
                onChange={(e) => set('at', e.target.value)}
                className="w-32 rounded-md border border-border bg-surface px-3 py-1.5 text-sm aria-[invalid=true]:border-danger"
              />
            </label>
          ) : (
            <label className="flex flex-col gap-1 text-xs text-foreground-muted">
              <span>At minute</span>
              <input
                type="text"
                aria-label="Minute of the hour"
                value={value.at}
                aria-invalid={shownError ? true : undefined}
                aria-describedby={shownError ? errId : undefined}
                onChange={(e) => set('at', e.target.value)}
                placeholder="HH:MM"
                className="w-32 rounded-md border border-border bg-surface px-3 py-1.5 text-sm aria-[invalid=true]:border-danger"
              />
            </label>
          )}
        </div>
      ) : (
        <label htmlFor={cronId} className="flex flex-col gap-1 text-xs text-foreground-muted">
          <span>Cron expression (minute hour day month weekday)</span>
          <input
            id={cronId}
            type="text"
            value={value.cron}
            aria-invalid={shownError ? true : undefined}
            aria-describedby={shownError ? errId : undefined}
            onChange={(e) => set('cron', e.target.value)}
            placeholder="0 8 * * 1"
            spellCheck={false}
            className="w-full max-w-sm rounded-md border border-border bg-surface px-3 py-1.5 font-mono text-sm aria-[invalid=true]:border-danger"
          />
        </label>
      )}

      {shownError ? (
        <p id={errId} role="alert" className="flex items-center gap-1.5 text-xs text-danger">
          <Icon name="alert-triangle" className="shrink-0" />
          {shownError}
        </p>
      ) : (
        <p className="text-xs text-foreground-muted">
          {value.mode === 'cron'
            ? 'Five space-separated fields. Example: “0 8 * * 1” = 08:00 every Monday.'
            : 'Fires in the timezone below (DST-correct).'}
        </p>
      )}
    </fieldset>
  );
}

function ModeChip({
  active,
  label,
  onClick,
  disabled,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      disabled={disabled}
      className={
        active
          ? 'rounded-md border border-accent bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent'
          : 'rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted'
      }
    >
      {label}
    </button>
  );
}
