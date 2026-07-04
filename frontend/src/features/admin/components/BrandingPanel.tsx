/**
 * BrandingPanel — the per-tenant application logo (admin branding, area:ui).
 *
 * A WRITE panel on the admin console: it shows the tenant's current logo (from the
 * principal the app already loads, GET /auth/me `logo_url`), lets an admin pick an
 * image (png/jpeg/svg, ≤ 1 MiB), preview it, and Upload it (PUT /admin/branding),
 * or Remove the current logo (DELETE /admin/branding) to revert to the default
 * brand mark. Both mutations invalidate the `/auth/me` query, so the shell brand
 * cell and this panel update together.
 *
 * All admin-only + tenant-scoped: a non-admin caller (403, INV-5) or expired
 * session (401, INV-4) surfaces as the shared write error, never a blank pane.
 * Every async state is handled (frontend/AGENTS.md: "every state, not just
 * success"): the current logo's loading/empty, client-side validation of the file
 * (type + size) before the PUT, a saving indicator, and a dismissible success/error
 * toast so an audited action's outcome is always visible.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { useCurrentUser } from '@/features/auth';
import { useClearTenantBranding, useUpdateTenantBranding } from '../model/queries';

/** Content-types the branding endpoint allowlists (LOGO_ALLOWED_CONTENT_TYPES). */
const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/svg+xml'];
const ACCEPT_ATTR = ACCEPTED_TYPES.join(',');
/** Server cap is MAX_LOGO_BYTES (1 MiB); mirror it client-side for a friendly error. */
const MAX_LOGO_BYTES = 1 * 1024 * 1024;

/** Best human-readable message for a write error (prefers the RFC-9457 Problem). */
function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to change the logo.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 413) return 'That image is too large. The logo must be 1 MiB or smaller.';
    if (error.status === 415) return 'Unsupported image type. Use a PNG, JPEG, or SVG.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not update the logo.';
}

/** Client-side validation mirroring the server's allowlist + size cap. */
function validateFile(file: File): string | null {
  if (!ACCEPTED_TYPES.includes(file.type)) {
    return 'Unsupported image type. Use a PNG, JPEG, or SVG.';
  }
  if (file.size > MAX_LOGO_BYTES) {
    return 'That image is too large. The logo must be 1 MiB or smaller.';
  }
  return null;
}

export function BrandingPanel() {
  const me = useCurrentUser();
  const uploadMutation = useUpdateTenantBranding();
  const clearMutation = useClearTenantBranding();

  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Mint (and revoke) an object URL for the selected file's preview.
  useEffect(() => {
    if (file === null) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const currentLogoUrl = me.data?.logo_url ?? null;
  const saving = uploadMutation.isPending || clearMutation.isPending;

  const handleSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    setToast(null);
    const next = event.target.files?.[0] ?? null;
    if (next === null) {
      setFile(null);
      return;
    }
    const problem = validateFile(next);
    if (problem !== null) {
      setToast({ kind: 'error', message: problem });
      setFile(null);
      if (inputRef.current) inputRef.current.value = '';
      return;
    }
    setFile(next);
  };

  const resetInput = () => {
    setFile(null);
    if (inputRef.current) inputRef.current.value = '';
  };

  const handleUpload = () => {
    if (file === null) return;
    setToast(null);
    uploadMutation.mutate(file, {
      onSuccess: () => {
        setToast({ kind: 'ok', message: 'Logo updated.' });
        resetInput();
      },
      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  const handleRemove = () => {
    setToast(null);
    clearMutation.mutate(undefined, {
      onSuccess: () => {
        setToast({ kind: 'ok', message: 'Logo removed. The default mark is now shown.' });
        resetInput();
      },
      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  return (
    <section aria-labelledby="admin-branding-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-branding-heading" className="text-sm font-semibold text-foreground">
          Application logo
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Upload a logo shown in the top-left of the app for everyone in this tenant. PNG,
          JPEG, or SVG, up to 1 MiB. Remove it to fall back to the default mark.
        </p>
      </header>

      <div className="space-y-5 p-4">
        {toast ? (
          <div
            role={toast.kind === 'error' ? 'alert' : 'status'}
            className={
              toast.kind === 'error'
                ? 'rounded-md border border-danger/40 bg-danger/5 p-3 text-sm'
                : 'rounded-md border border-border bg-surface-muted p-3 text-sm'
            }
          >
            <div className="flex items-center justify-between gap-3">
              <span className="text-foreground">{toast.message}</span>
              <button
                type="button"
                onClick={() => setToast(null)}
                className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface"
              >
                Dismiss
              </button>
            </div>
          </div>
        ) : null}

        {/* Current logo */}
        <div className="flex items-center gap-4">
          <div
            className="flex h-16 w-16 flex-none items-center justify-center overflow-hidden rounded-md border border-border bg-surface-muted"
            aria-hidden="true"
          >
            {me.isLoading ? (
              <span className="text-xs text-foreground-muted">…</span>
            ) : currentLogoUrl ? (
              <img
                src={currentLogoUrl}
                alt=""
                className="h-full w-full object-contain"
                data-testid="current-logo"
              />
            ) : (
              <span className="text-xs text-foreground-muted">Default</span>
            )}
          </div>
          <div className="text-sm">
            <div className="font-medium text-foreground">Current logo</div>
            <div className="text-xs text-foreground-muted">
              {me.isLoading
                ? 'Loading…'
                : me.isError
                  ? 'Could not load the current logo.'
                  : currentLogoUrl
                    ? 'A custom logo is set for this tenant.'
                    : 'No custom logo — the default mark is shown.'}
            </div>
          </div>
        </div>

        {/* File picker + preview */}
        <div className="flex flex-col gap-2">
          <label htmlFor="branding-file" className="text-xs font-medium text-foreground">
            Choose an image
          </label>
          <input
            id="branding-file"
            ref={inputRef}
            type="file"
            accept={ACCEPT_ATTR}
            disabled={saving}
            onChange={handleSelect}
            className="block w-full text-sm text-foreground file:mr-3 file:rounded-md file:border file:border-border file:bg-surface file:px-3 file:py-1.5 file:text-sm file:font-medium hover:file:bg-surface-muted disabled:opacity-50"
          />
          {previewUrl ? (
            <div className="mt-1 flex items-center gap-3">
              <span className="text-xs text-foreground-muted">Preview:</span>
              <img
                src={previewUrl}
                alt="Selected logo preview"
                data-testid="logo-preview"
                className="h-12 w-12 rounded-md border border-border object-contain"
              />
            </div>
          ) : null}
        </div>

        {/* Actions */}
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={handleUpload}
            disabled={saving || file === null}
            className="rounded-md border border-accent bg-accent/10 px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent/20 disabled:opacity-50"
          >
            {uploadMutation.isPending ? 'Uploading…' : 'Upload logo'}
          </button>
          <button
            type="button"
            onClick={handleRemove}
            disabled={saving || (!currentLogoUrl && !me.isLoading)}
            className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-muted disabled:opacity-50"
          >
            {clearMutation.isPending ? 'Removing…' : 'Remove logo'}
          </button>
        </div>
      </div>
    </section>
  );
}
