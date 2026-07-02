/**
 * TimezoneInput (#237, AC-4) — an IANA timezone picker with client-side
 * validation. When the platform exposes `Intl.supportedValuesOf('timeZone')` we
 * render a native datalist of real zone names; otherwise a free-text input still
 * works, validated against `Intl.DateTimeFormat` (which mirrors the server's
 * zoneinfo check — an unknown name → 422 `invalid_timezone`). Friendly error
 * message on an unknown name.
 */
import { useId, useMemo } from 'react';
import { Icon } from '@/ui';
import { knownTimezones, validateTimezone } from '../model/cadence';

interface TimezoneInputProps {
  value: string;
  onChange: (tz: string) => void;
  disabled?: boolean;
  /** A submit-time error surfaced by the parent (e.g. a 422 from the server). */
  error?: string | null;
}

export function TimezoneInput({ value, onChange, disabled, error }: TimezoneInputProps) {
  const inputId = useId();
  const listId = `${inputId}-zones`;
  const errId = `${inputId}-error`;
  const zones = useMemo(() => knownTimezones(), []);
  const liveError = validateTimezone(value);
  const shownError = error ?? liveError;

  return (
    <div className="min-w-0">
      <label htmlFor={inputId} className="mb-1 block text-sm font-medium">
        Timezone
      </label>
      <input
        id={inputId}
        type="text"
        list={zones.length > 0 ? listId : undefined}
        value={value}
        disabled={disabled}
        aria-invalid={shownError ? true : undefined}
        aria-describedby={shownError ? errId : `${inputId}-hint`}
        onChange={(e) => onChange(e.target.value)}
        placeholder="America/New_York"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className="w-full max-w-sm rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
      />
      {zones.length > 0 ? (
        <datalist id={listId}>
          {zones.map((z) => (
            <option key={z} value={z} />
          ))}
        </datalist>
      ) : null}
      {shownError ? (
        <p id={errId} role="alert" className="mt-1 flex items-center gap-1.5 text-xs text-danger">
          <Icon name="alert-triangle" className="shrink-0" />
          {shownError}
        </p>
      ) : (
        <p id={`${inputId}-hint`} className="mt-1 text-xs text-foreground-muted">
          An IANA name (e.g. America/New_York). Drives the DST-correct next run.
        </p>
      )}
    </div>
  );
}
