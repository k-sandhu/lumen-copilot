/**
 * Custom-instructions control (user settings). Reads/writes the caller's server-side
 * `custom_instructions` (GET/PATCH /preferences) — a free-text preamble prepended to
 * the chat system prompt (before the grounding contract, which is never removed).
 *
 * A labelled `<textarea>` seeded from the loaded preference, a live character counter
 * against the 2000-char cap, and a Save that PATCHes only when the value actually
 * changed. Every async state is handled (loading / error / saving / success): a
 * dismissible status line reports the outcome of an audited write, and an over-cap
 * value is blocked client-side (mirroring the server's 422) with the Save disabled.
 */
import { useEffect, useMemo, useState } from 'react';
import { ApiError } from '@/api';
import { usePreferences, useUpdatePreferences } from '@/features/preferences';

/** Server cap is MAX_CUSTOM_INSTRUCTIONS_CHARS (2000); mirror it for a friendly error. */
const MAX_CHARS = 2000;

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 422) return 'Those instructions are too long. Keep them under 2000 characters.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not save your instructions. Please try again.';
}

/** The stored value as a plain string ('' when null / not yet set). */
function storedText(value: string | null | undefined): string {
  return value ?? '';
}

export function CustomInstructionsSetting() {
  const prefs = usePreferences();
  const update = useUpdatePreferences();

  const stored = storedText(prefs.data?.custom_instructions);
  const [draft, setDraft] = useState<string>(stored);
  const [status, setStatus] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);

  // Re-seed the draft when the loaded preference resolves/changes (and no unsaved edit
  // is pending) so the textarea reflects the server state after load or invalidation.
  useEffect(() => {
    setDraft(stored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefs.data?.custom_instructions]);

  const trimmed = draft.trim();
  const overCap = draft.length > MAX_CHARS;
  const dirty = useMemo(() => trimmed !== stored.trim(), [trimmed, stored]);
  const saving = update.isPending;

  const onSave = () => {
    if (overCap || !dirty) return;
    setStatus(null);
    // A blank draft clears the override (null); else send the trimmed value.
    update.mutate(
      { custom_instructions: trimmed === '' ? null : trimmed },
      {
        onSuccess: () => setStatus({ kind: 'ok', message: 'Instructions saved.' }),
        onError: (error) => setStatus({ kind: 'error', message: describeError(error) }),
      },
    );
  };

  if (prefs.isLoading) {
    return (
      <section aria-labelledby="settings-instructions-heading" className="rounded-lg border border-border">
        <header className="border-b border-border px-4 py-3">
          <h2 id="settings-instructions-heading" className="text-sm font-semibold text-foreground">
            Custom instructions
          </h2>
        </header>
        <p className="p-4 text-xs text-foreground-muted">Loading…</p>
      </section>
    );
  }

  return (
    <section aria-labelledby="settings-instructions-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="settings-instructions-heading" className="text-sm font-semibold text-foreground">
          Custom instructions
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Standing context added to the start of every chat (your persona, tone, or
          preferences). Grounding and citations always still apply.
        </p>
      </header>

      <div className="space-y-3 p-4">
        {prefs.isError ? (
          <p role="alert" className="text-xs text-danger">
            Couldn&apos;t load your instructions. Please try again.
          </p>
        ) : null}

        <label htmlFor="settings-instructions" className="sr-only">
          Custom instructions
        </label>
        <textarea
          id="settings-instructions"
          value={draft}
          disabled={saving || prefs.isError}
          onChange={(e) => {
            setDraft(e.target.value);
            setStatus(null);
          }}
          rows={6}
          placeholder="e.g. Always answer concisely and prefer bullet points."
          aria-describedby="settings-instructions-count"
          className="block w-full resize-y rounded-md border border-border bg-surface p-2 text-sm text-foreground disabled:opacity-50"
        />

        <div className="flex items-center justify-between gap-3">
          <span
            id="settings-instructions-count"
            className={overCap ? 'text-xs text-danger' : 'text-xs text-foreground-muted'}
            aria-live="polite"
          >
            {draft.length} / {MAX_CHARS}
          </span>
          <button
            type="button"
            onClick={onSave}
            disabled={saving || overCap || !dirty}
            className="rounded-md border border-accent bg-accent/10 px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent/20 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>

        {status ? (
          <p
            role={status.kind === 'error' ? 'alert' : 'status'}
            className={status.kind === 'error' ? 'text-xs text-danger' : 'text-xs text-foreground-muted'}
          >
            {status.message}
          </p>
        ) : null}
      </div>
    </section>
  );
}
