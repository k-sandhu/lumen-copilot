/**
 * AddSourceModal (#27, ADR-0009 §5) — paste a public URL → `POST /sources`. The
 * `web` connector needs zero source-side setup: the body is `{type: 'web', url}`.
 *
 * Flow: validate the URL client-side (a UX guard — http/https + parseable), then
 * submit. On success the new source appears in the grid as `pending` (the list is
 * invalidated by the mutation) and the modal closes. On a 422 (invalid OR
 * SSRF-blocked URL — the server runs the authoritative check, ADR-0009 §3) the
 * message renders INLINE under the field and the modal stays open so the user can
 * fix the link.
 *
 * A11y: role="dialog" aria-modal, labelled by its title; focus moves to the URL
 * field on open and is restored to the trigger on close; Escape and a backdrop
 * click dismiss; the field is described by its error via aria-describedby and
 * marked aria-invalid so screen-reader users hear the rejection.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useCreateSource } from '../model/queries';
import { createSourceErrorMessage, validateUrl } from '../model/presentation';

interface AddSourceModalProps {
  open: boolean;
  onClose: () => void;
}

export function AddSourceModal({ open, onClose }: AddSourceModalProps) {
  const titleId = useId();
  const errorId = useId();
  const hintId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const lastFocused = useRef<HTMLElement | null>(null);

  const [url, setUrl] = useState('');
  // A client-side validation message, kept separate from the server error so the
  // two never fight: client error clears the moment the user edits the field.
  const [clientError, setClientError] = useState<string | null>(null);

  const create = useCreateSource();
  const serverError =
    create.error instanceof ApiError ? createSourceErrorMessage(create.error) : null;
  const error = clientError ?? serverError;

  // Manage focus + Escape; restore focus to the trigger on close. Reset the form
  // each time the modal opens so a prior error/url never leaks into a new attempt.
  useEffect(() => {
    if (!open) return;
    lastFocused.current = (document.activeElement as HTMLElement) ?? null;
    setUrl('');
    setClientError(null);
    create.reset();
    // Defer focus to the next frame so the field is mounted.
    const id = window.requestAnimationFrame(() => inputRef.current?.focus());
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(id);
      window.removeEventListener('keydown', onKey);
      lastFocused.current?.focus?.();
    };
    // create.reset is stable; intentionally run only on open/close transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onClose]);

  if (!open) return null;

  const submitting = create.isPending;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    const result = validateUrl(url);
    if (!result.ok || !result.url) {
      setClientError(result.error ?? 'Enter a valid URL.');
      inputRef.current?.focus();
      return;
    }
    setClientError(null);
    create.mutate(
      { type: 'web', url: result.url },
      {
        onSuccess: () => onClose(),
        // On error we keep the modal open; the message renders inline via serverError.
        onError: () => inputRef.current?.focus(),
      },
    );
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-[12vh]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-surface shadow-xl">
        <header className="flex items-center gap-3 border-b border-border px-5 py-3.5">
          <h2 id={titleId} className="text-sm font-semibold">
            Add a source
          </h2>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            aria-label="Close"
            className="ml-auto rounded-md border border-border p-1.5 text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            <Icon name="x" />
          </button>
        </header>

        <form onSubmit={handleSubmit} noValidate>
          <div className="space-y-3 px-5 py-4">
            <p className="text-sm text-foreground-muted">
              Paste a public link — a web page, an RSS/Atom feed, or a sitemap. Lumen ingests it,
              and only you (within your tenant) can chat over it.
            </p>

            <div>
              <label htmlFor={`${titleId}-url`} className="mb-1 block text-xs font-medium">
                Link
              </label>
              <input
                ref={inputRef}
                id={`${titleId}-url`}
                type="url"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                placeholder="https://example.com/handbook"
                value={url}
                disabled={submitting}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? errorId : hintId}
                onChange={(e) => {
                  setUrl(e.target.value);
                  if (clientError) setClientError(null);
                  if (serverError) create.reset();
                }}
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
              />
              {error ? (
                <p id={errorId} role="alert" className="mt-1.5 flex items-start gap-1.5 text-xs text-danger">
                  <Icon name="alert-triangle" className="mt-px shrink-0" />
                  <span>{error}</span>
                </p>
              ) : (
                <p id={hintId} className="mt-1.5 text-xs text-foreground-muted">
                  http:// and https:// links only. We never widen access — the source is yours alone.
                </p>
              )}
            </div>
          </div>

          <footer className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
            >
              {submitting ? 'Adding…' : 'Add source'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
