/**
 * AddSourceModal (#27, ADR-0009 §5; #455, ADR-0019 §1/§5) — add a source.
 *
 * `web` (any member): paste a public URL → `POST /sources {type:'web', url}`.
 * Client-side validation is a UX guard; the server runs the authoritative SSRF
 * check (ADR-0009 §3) and a 422 renders INLINE so the user can fix the link.
 *
 * `gdrive` (TENANT ADMIN only — the connector choice is not even rendered for a
 * non-admin, INV-5): pick what to sync (My Drive / a folder / a Shared Drive —
 * the closed mode-discriminated config variants), then
 *   `POST /sources {type:'gdrive', config}` → 201 `pending_auth`
 *   `POST /sources/{id}/connect`            → `{authorization_url}`
 * and the BROWSER navigates to the provider consent screen. The provider
 * redirects back to /sources with the frozen `connect=ok|error` params. If the
 * connect step fails after the create succeeded, the source exists in the grid
 * as `pending_auth` with its own Connect action — the inline error says so.
 *
 * A11y: role="dialog" aria-modal, labelled by its title; focus moves into the
 * dialog on open and is restored to the trigger on close; Escape and a backdrop
 * click dismiss; fields are described by their errors via aria-describedby and
 * marked aria-invalid so screen-reader users hear the rejection.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useFocusTrap } from '@/lib/useFocusTrap';
import { useConnectSource, useCreateSource } from '../model/queries';
import {
  connectSourceErrorMessage,
  createSourceErrorMessage,
  validateUrl,
} from '../model/presentation';
import { navigateToConsent } from '../model/browser';
import type { GdriveSourceConfig } from '../model/types';

interface AddSourceModalProps {
  open: boolean;
  onClose: () => void;
  /** Whether the CALLER is a tenant admin — gates the Google Drive connector (INV-5). */
  isAdmin?: boolean;
}

type ConnectorKind = 'web' | 'gdrive';
type GdriveMode = GdriveSourceConfig['mode'];

export function AddSourceModal({ open, onClose, isAdmin = false }: AddSourceModalProps) {
  const titleId = useId();
  const errorId = useId();
  const hintId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const folderIdRef = useRef<HTMLInputElement>(null);
  const driveIdRef = useRef<HTMLInputElement>(null);

  const [kind, setKind] = useState<ConnectorKind>('web');
  const [url, setUrl] = useState('');
  const [mode, setMode] = useState<GdriveMode>('my_drive');
  const [folderId, setFolderId] = useState('');
  const [driveId, setDriveId] = useState('');
  // A client-side validation message, kept separate from the server error so the
  // two never fight: client error clears the moment the user edits a field.
  const [clientError, setClientError] = useState<string | null>(null);

  const create = useCreateSource();
  const connect = useConnectSource();
  const serverError =
    create.error instanceof ApiError
      ? createSourceErrorMessage(create.error)
      : connect.error instanceof ApiError
        ? `${connectSourceErrorMessage(connect.error)} The source was created — you can retry Connect from its card.`
        : null;
  const error = clientError ?? serverError;

  const submitting = create.isPending || connect.isPending;

  // Reset the form each time the modal opens so a prior error/config never leaks
  // into a new attempt. (Focus/Tab-trap/Escape/restore is handled below by the
  // shared useFocusTrap hook.)
  useEffect(() => {
    if (!open) return;
    setKind('web');
    setUrl('');
    setMode('my_drive');
    setFolderId('');
    setDriveId('');
    setClientError(null);
    create.reset();
    connect.reset();
    // create.reset/connect.reset are stable; intentionally run only on the open
    // transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Move focus into the dialog on open, trap Tab inside it, restore focus to
  // the trigger on close. The Tab trap stays ACTIVE while submitting — a
  // slow/hung POST must not let focus escape behind the overlay — so only the
  // Escape dismiss is gated on `submitting` (mirrors ConfirmDialog's `busy`).
  // The close callback must be REFERENTIALLY STABLE across the modal's own
  // re-renders: the trap effect depends on it, and re-running the effect
  // re-fires the initial-focus move — which would steal focus from whichever
  // gdrive field the admin is typing in (`submitting` rides a ref for the same
  // reason).
  const submittingRef = useRef(submitting);
  submittingRef.current = submitting;
  const requestClose = useCallback(() => {
    if (!submittingRef.current) onClose();
  }, [onClose]);
  useFocusTrap(open, dialogRef, requestClose, { initialFocus: inputRef });

  if (!open) return null;

  const resetServerErrors = () => {
    if (create.error) create.reset();
    if (connect.error) connect.reset();
  };

  function submitWeb() {
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

  /** Build the closed mode-discriminated config, or flag the missing id. */
  function buildGdriveConfig(): GdriveSourceConfig | null {
    if (mode === 'my_drive') return { mode: 'my_drive' };
    if (mode === 'folder') {
      const folder = folderId.trim();
      if (!folder) {
        setClientError('Enter the Drive folder id to sync.');
        folderIdRef.current?.focus();
        return null;
      }
      const drive = driveId.trim();
      return drive ? { mode: 'folder', folder_id: folder, drive_id: drive } : { mode: 'folder', folder_id: folder };
    }
    const drive = driveId.trim();
    if (!drive) {
      setClientError('Enter the Shared Drive id to sync.');
      driveIdRef.current?.focus();
      return null;
    }
    return { mode: 'shared_drive', drive_id: drive };
  }

  function submitGdrive() {
    const config = buildGdriveConfig();
    if (!config) return;
    setClientError(null);
    // Create (201 pending_auth), then start the consent flow and leave for the
    // provider. Two admin-gated T1 writes (ADR-0019 §1); either 403 renders
    // inline — an error state, never a blank pane.
    create.mutate(
      { type: 'gdrive', config },
      {
        onSuccess: (source) => {
          connect.mutate(source.id, {
            onSuccess: (res) => navigateToConsent(res.authorization_url),
          });
        },
      },
    );
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    if (kind === 'gdrive') submitGdrive();
    else submitWeb();
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      // Programmatically focusable so the focus-trap's zero-focusable fallback
      // (`container.focus()`) actually works while every control is disabled
      // during a pending create/connect — without this, Tab could escape to
      // the page behind the overlay.
      tabIndex={-1}
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
            {/* Connector picker — the managed gdrive option renders ONLY for a
                tenant admin (INV-5: a non-admin sees no managed affordance). */}
            {isAdmin ? (
              <fieldset>
                <legend className="mb-1.5 text-xs font-medium">Connector</legend>
                <div className="grid grid-cols-2 gap-2">
                  <ConnectorOption
                    checked={kind === 'web'}
                    disabled={submitting}
                    label="Web link"
                    description="A page, feed, or sitemap"
                    onSelect={() => {
                      setKind('web');
                      setClientError(null);
                      resetServerErrors();
                    }}
                  />
                  <ConnectorOption
                    checked={kind === 'gdrive'}
                    disabled={submitting}
                    label="Google Drive"
                    description="Read-only · permissions mirrored"
                    onSelect={() => {
                      setKind('gdrive');
                      setClientError(null);
                      resetServerErrors();
                    }}
                  />
                </div>
              </fieldset>
            ) : null}

            {kind === 'web' ? (
              <>
                <p className="text-sm text-foreground-muted">
                  Paste a public link — a web page, an RSS/Atom feed, or a sitemap. Lumen ingests
                  it, and only you (within your tenant) can chat over it.
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
                      resetServerErrors();
                    }}
                    className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
                  />
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-foreground-muted">
                  Connect Google Drive read-only. You&rsquo;ll be sent to Google to consent; each
                  file&rsquo;s own permissions are mirrored — access is never widened.
                </p>

                <fieldset>
                  <legend className="mb-1.5 text-xs font-medium">What to sync</legend>
                  <div className="space-y-1.5">
                    <ScopeOption
                      checked={mode === 'my_drive'}
                      disabled={submitting}
                      label="My Drive"
                      description="Everything in the connected account's My Drive"
                      onSelect={() => {
                        setMode('my_drive');
                        setClientError(null);
                        resetServerErrors();
                      }}
                    />
                    <ScopeOption
                      checked={mode === 'folder'}
                      disabled={submitting}
                      label="A folder"
                      description="One Drive folder (optionally inside a Shared Drive)"
                      onSelect={() => {
                        setMode('folder');
                        setClientError(null);
                        resetServerErrors();
                      }}
                    />
                    <ScopeOption
                      checked={mode === 'shared_drive'}
                      disabled={submitting}
                      label="A Shared Drive"
                      description="A whole Shared Drive"
                      onSelect={() => {
                        setMode('shared_drive');
                        setClientError(null);
                        resetServerErrors();
                      }}
                    />
                  </div>
                </fieldset>

                {mode === 'folder' ? (
                  <>
                    <div>
                      <label htmlFor={`${titleId}-folder`} className="mb-1 block text-xs font-medium">
                        Folder id
                      </label>
                      <input
                        ref={folderIdRef}
                        id={`${titleId}-folder`}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder="1AbCdEfGhIjKlMnOp"
                        value={folderId}
                        disabled={submitting}
                        aria-invalid={error ? true : undefined}
                        aria-describedby={error ? errorId : undefined}
                        onChange={(e) => {
                          setFolderId(e.target.value);
                          if (clientError) setClientError(null);
                          resetServerErrors();
                        }}
                        className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
                      />
                    </div>
                    <div>
                      <label htmlFor={`${titleId}-folder-drive`} className="mb-1 block text-xs font-medium">
                        Shared Drive id{' '}
                        <span className="font-normal text-foreground-muted">
                          (only if the folder lives in a Shared Drive)
                        </span>
                      </label>
                      <input
                        id={`${titleId}-folder-drive`}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder="0AbCdEfGhIjKl"
                        value={driveId}
                        disabled={submitting}
                        onChange={(e) => {
                          setDriveId(e.target.value);
                          if (clientError) setClientError(null);
                          resetServerErrors();
                        }}
                        className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
                      />
                    </div>
                  </>
                ) : null}

                {mode === 'shared_drive' ? (
                  <div>
                    <label htmlFor={`${titleId}-drive`} className="mb-1 block text-xs font-medium">
                      Shared Drive id
                    </label>
                    <input
                      ref={driveIdRef}
                      id={`${titleId}-drive`}
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="0AbCdEfGhIjKl"
                      value={driveId}
                      disabled={submitting}
                      aria-invalid={error ? true : undefined}
                      aria-describedby={error ? errorId : undefined}
                      onChange={(e) => {
                        setDriveId(e.target.value);
                        if (clientError) setClientError(null);
                        resetServerErrors();
                      }}
                      className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
                    />
                  </div>
                ) : null}
              </>
            )}

            {error ? (
              <p id={errorId} role="alert" className="mt-1.5 flex items-start gap-1.5 text-xs text-danger">
                <Icon name="alert-triangle" className="mt-px shrink-0" />
                <span>{error}</span>
              </p>
            ) : (
              <p id={hintId} className="mt-1.5 text-xs text-foreground-muted">
                {kind === 'web'
                  ? 'http:// and https:// links only. We never widen access — the source is yours alone.'
                  : "Find the id in the folder or Shared Drive's URL. After Google consent the first sync starts automatically."}
              </p>
            )}
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
              {kind === 'gdrive'
                ? submitting
                  ? connect.isPending
                    ? 'Redirecting to Google…'
                    : 'Creating…'
                  : 'Continue to Google'
                : submitting
                  ? 'Adding…'
                  : 'Add source'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}

/** One connector-type radio card. */
function ConnectorOption({
  checked,
  disabled,
  label,
  description,
  onSelect,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  description: string;
  onSelect: () => void;
}) {
  return (
    <label
      className={`flex cursor-pointer flex-col gap-0.5 rounded-md border px-3 py-2 text-sm ${
        checked ? 'border-accent ring-1 ring-accent' : 'border-border hover:bg-surface-muted'
      } ${disabled ? 'opacity-60' : ''}`}
    >
      <span className="flex items-center gap-2">
        <input
          type="radio"
          name="connector-kind"
          checked={checked}
          disabled={disabled}
          onChange={onSelect}
          className="accent-[rgb(var(--c-accent))]"
        />
        <span className="font-medium">{label}</span>
      </span>
      <span className="pl-6 text-xs text-foreground-muted">{description}</span>
    </label>
  );
}

/** One gdrive sync-scope radio row. */
function ScopeOption({
  checked,
  disabled,
  label,
  description,
  onSelect,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  description: string;
  onSelect: () => void;
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 text-sm ${
        checked ? 'border-accent ring-1 ring-accent' : 'border-border hover:bg-surface-muted'
      } ${disabled ? 'opacity-60' : ''}`}
    >
      <input
        type="radio"
        name="gdrive-scope"
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
        className="mt-0.5 accent-[rgb(var(--c-accent))]"
      />
      <span className="min-w-0">
        <span className="block font-medium">{label}</span>
        <span className="block text-xs text-foreground-muted">{description}</span>
      </span>
    </label>
  );
}
