/**
 * RegisterServerModal (#228, ADR-0012 §5) — register a remote MCP server:
 * `POST /mcp-servers` with `{name, transport, endpoint_url, auth?}`.
 *
 * Fields: display name, transport (streamable_http | sse), endpoint URL (client
 * validated as an absolute https URL — a UX guard; the server runs the
 * authoritative https + SSRF check), and a WRITE-ONLY secret field. The secret is
 * never pre-filled and never re-displayed: this form only ever SENDS a value, it
 * never renders a stored one (AC-2 / CC-C #209). An empty secret registers with no
 * auth.
 *
 * On success the new server appears in the grid as `pending` (the list is
 * invalidated by the mutation) and the modal closes. On a 422 (unsupported
 * transport / invalid / non-https / SSRF-blocked endpoint — the server runs the
 * authoritative check, ADR-0012 §1/§4) the reason renders INLINE and the modal
 * stays open so the user can fix it.
 *
 * A11y: role="dialog" aria-modal, labelled by its title; focus moves to the name
 * field on open and is restored to the trigger on close; Escape and backdrop
 * dismiss; each field is described by its error via aria-describedby and marked
 * aria-invalid so screen-reader users hear the rejection.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useFocusTrap } from '@/lib/useFocusTrap';
import { useRegisterMcpServer } from '../model/queries';
import { registerErrorMessage, validateEndpoint } from '../model/presentation';
import type { McpServerAuth, McpTransport } from '../model/types';

interface RegisterServerModalProps {
  open: boolean;
  onClose: () => void;
}

const TRANSPORTS: { value: McpTransport; label: string; hint: string }[] = [
  { value: 'streamable_http', label: 'Streamable HTTP', hint: 'Recommended' },
  { value: 'sse', label: 'Server-Sent Events (SSE)', hint: 'Compatibility' },
];

export function RegisterServerModal({ open, onClose }: RegisterServerModalProps) {
  const titleId = useId();
  const errorId = useId();
  const endpointHintId = useId();
  const secretHintId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const [name, setName] = useState('');
  const [transport, setTransport] = useState<McpTransport>('streamable_http');
  const [endpoint, setEndpoint] = useState('');
  // Write-only: this holds the value the user is TYPING to send; it is never
  // seeded from any stored value and is cleared on every open. Sending it is the
  // only thing that ever happens to it.
  const [secret, setSecret] = useState('');
  // A client-side validation message, kept separate from the server error so the
  // two never fight: client error clears the moment the user edits a field.
  const [clientError, setClientError] = useState<string | null>(null);

  const register = useRegisterMcpServer();
  const serverError =
    register.error instanceof ApiError ? registerErrorMessage(register.error) : null;
  const error = clientError ?? serverError;

  const submitting = register.isPending;

  // Track `submitting` in a ref so the focus-trap close handler stays stable
  // across renders — a new closure each keystroke would re-run the trap effect and
  // steal focus back to the first field mid-typing.
  const submittingRef = useRef(submitting);
  submittingRef.current = submitting;

  // Reset the form each time the modal opens so a prior error / value never leaks
  // into a new attempt — the secret in particular is always blank on open.
  useEffect(() => {
    if (!open) return;
    setName('');
    setTransport('streamable_http');
    setEndpoint('');
    setSecret('');
    setClientError(null);
    register.reset();
    // register.reset is stable; intentionally run only on the open transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleTrapClose = useCallback(() => {
    if (!submittingRef.current) onClose();
  }, [onClose]);
  useFocusTrap(open, dialogRef, handleTrapClose, { initialFocus: nameRef });

  if (!open) return null;

  function clearServerError() {
    if (clientError) setClientError(null);
    if (serverError) register.reset();
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;

    if (!name.trim()) {
      setClientError('Give the server a name.');
      nameRef.current?.focus();
      return;
    }
    const endpointResult = validateEndpoint(endpoint);
    if (!endpointResult.ok || !endpointResult.url) {
      setClientError(endpointResult.error ?? 'Enter a valid https endpoint URL.');
      return;
    }
    setClientError(null);

    const trimmedSecret = secret.trim();
    const auth: McpServerAuth | undefined = trimmedSecret
      ? { type: 'bearer', value: trimmedSecret }
      : undefined;

    register.mutate(
      { name: name.trim(), transport, endpoint_url: endpointResult.url, auth },
      {
        onSuccess: () => onClose(),
        onError: () => nameRef.current?.focus(),
      },
    );
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-[10vh]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-surface shadow-xl">
        <header className="flex items-center gap-3 border-b border-border px-5 py-3.5">
          <h2 id={titleId} className="text-sm font-semibold">
            Register an MCP server
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
          <div className="space-y-4 px-5 py-4">
            <p className="text-sm text-foreground-muted">
              Connect a remote MCP server. We’ll register it (pending), then you can test it to
              discover its tools. Its tools stay yours alone within your tenant.
            </p>

            {/* Name */}
            <div>
              <label htmlFor={`${titleId}-name`} className="mb-1 block text-xs font-medium">
                Name
              </label>
              <input
                ref={nameRef}
                id={`${titleId}-name`}
                type="text"
                autoComplete="off"
                placeholder="Acme Ticketing"
                value={name}
                disabled={submitting}
                maxLength={200}
                onChange={(e) => {
                  setName(e.target.value);
                  clearServerError();
                }}
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              />
            </div>

            {/* Transport */}
            <div>
              <label htmlFor={`${titleId}-transport`} className="mb-1 block text-xs font-medium">
                Transport
              </label>
              <select
                id={`${titleId}-transport`}
                value={transport}
                disabled={submitting}
                onChange={(e) => {
                  setTransport(e.target.value as McpTransport);
                  clearServerError();
                }}
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              >
                {TRANSPORTS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label} — {t.hint}
                  </option>
                ))}
              </select>
            </div>

            {/* Endpoint */}
            <div>
              <label htmlFor={`${titleId}-endpoint`} className="mb-1 block text-xs font-medium">
                Endpoint URL
              </label>
              <input
                id={`${titleId}-endpoint`}
                type="url"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                placeholder="https://mcp.example.com/sse"
                value={endpoint}
                disabled={submitting}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? errorId : endpointHintId}
                onChange={(e) => {
                  setEndpoint(e.target.value);
                  clearServerError();
                }}
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
              />
              {!error ? (
                <p id={endpointHintId} className="mt-1.5 text-xs text-foreground-muted">
                  https:// only. Private, loopback, and cloud-metadata addresses are blocked.
                </p>
              ) : null}
            </div>

            {/* Secret — write-only */}
            <div>
              <label htmlFor={`${titleId}-secret`} className="mb-1 block text-xs font-medium">
                Secret <span className="font-normal text-foreground-muted">(optional)</span>
              </label>
              <input
                id={`${titleId}-secret`}
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                placeholder="Bearer token — sent once, never shown again"
                value={secret}
                disabled={submitting}
                aria-describedby={secretHintId}
                onChange={(e) => {
                  setSecret(e.target.value);
                  clearServerError();
                }}
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              />
              <p id={secretHintId} className="mt-1.5 text-xs text-foreground-muted">
                Stored write-only and encrypted — we never display it back. Leave blank for an
                unauthenticated server.
              </p>
            </div>

            {error ? (
              <p
                id={errorId}
                role="alert"
                className="flex items-start gap-1.5 text-xs text-danger"
              >
                <Icon name="alert-triangle" className="mt-px shrink-0" />
                <span>{error}</span>
              </p>
            ) : null}
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
              {submitting ? 'Registering…' : 'Register server'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
