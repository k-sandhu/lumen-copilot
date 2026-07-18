/**
 * AvatarSetting — the per-user profile picture (user settings).
 *
 * Shows the caller's current avatar (from the principal the app already loads, GET
 * /auth/me `avatar_url`), lets the user pick an image (png/jpeg/svg, ≤ 1 MiB), preview
 * it, and Upload it (PUT /me/avatar), or Remove the current avatar (DELETE /me/avatar)
 * to fall back to their initials. Both mutations invalidate the `/auth/me` query, so
 * the shell (AccountMenu) and this section update together.
 *
 * Every async state is handled (frontend/AGENTS.md: "every state, not just success"):
 * the current avatar's loading/empty, client-side validation of the file (type + size)
 * before the PUT, a saving indicator, and a dismissible success/error toast so an
 * audited action's outcome is always visible.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { useCurrentUser } from '@/features/auth';
import { useClearAvatar, useUpdateAvatar } from '../model/queries';

/** Content-types the avatar endpoint allowlists (reused LOGO_ALLOWED_CONTENT_TYPES). */
const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/svg+xml'];
const ACCEPT_ATTR = ACCEPTED_TYPES.join(',');
/** Server cap is MAX_LOGO_BYTES (1 MiB, reused for avatars); mirror it client-side. */
const MAX_AVATAR_BYTES = 1 * 1024 * 1024;

function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 413)
      return 'That image is too large. The avatar must be 1 MiB or smaller.';
    if (error.status === 415) return 'Unsupported image type. Use a PNG, JPEG, or SVG.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not update your avatar.';
}

/** Client-side validation mirroring the server's allowlist + size cap. */
function validateFile(file: File): string | null {
  if (!ACCEPTED_TYPES.includes(file.type)) {
    return 'Unsupported image type. Use a PNG, JPEG, or SVG.';
  }
  if (file.size > MAX_AVATAR_BYTES) {
    return 'That image is too large. The avatar must be 1 MiB or smaller.';
  }
  return null;
}

/** Initials from the email, mirroring the shell AccountMenu fallback. */
function initials(email: string | undefined): string {
  if (!email) return '··';
  const name = email.split('@')[0] || email;
  const parts = name.split(/[.\-_]/).filter(Boolean);
  const letters =
    parts.length >= 2 ? `${parts[0]?.[0] ?? ''}${parts[1]?.[0] ?? ''}` : name.slice(0, 2);
  return letters.toUpperCase() || '··';
}

export function AvatarSetting() {
  const me = useCurrentUser();
  const uploadMutation = useUpdateAvatar();
  const clearMutation = useClearAvatar();

  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (file === null) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const currentAvatarUrl = me.data?.avatar_url ?? null;
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
        setToast({ kind: 'ok', message: 'Profile picture updated.' });
        resetInput();
      },
      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  const handleRemove = () => {
    setToast(null);
    clearMutation.mutate(undefined, {
      onSuccess: () => {
        setToast({ kind: 'ok', message: 'Profile picture removed. Your initials are now shown.' });
        resetInput();
      },
      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  return (
    <section aria-labelledby="settings-avatar-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="settings-avatar-heading" className="text-sm font-semibold text-foreground">
          Profile picture
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Shown next to your account in the app. PNG, JPEG, or SVG, up to 1 MiB. Remove it to fall
          back to your initials.
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

        {/* Current avatar */}
        <div className="flex items-center gap-4">
          <div
            className="flex h-16 w-16 flex-none items-center justify-center overflow-hidden rounded-full border border-border bg-surface-muted text-sm font-medium text-foreground-muted"
            aria-hidden="true"
          >
            {me.isLoading ? (
              <span className="text-xs">…</span>
            ) : currentAvatarUrl ? (
              <img
                src={currentAvatarUrl}
                alt=""
                className="h-full w-full object-cover"
                data-testid="current-avatar"
              />
            ) : (
              <span>{initials(me.data?.email)}</span>
            )}
          </div>
          <div className="text-sm">
            <div className="font-medium text-foreground">Current picture</div>
            <div className="text-xs text-foreground-muted">
              {me.isLoading
                ? 'Loading…'
                : me.isError
                  ? 'Could not load your current picture.'
                  : currentAvatarUrl
                    ? 'A custom picture is set.'
                    : 'No custom picture — your initials are shown.'}
            </div>
          </div>
        </div>

        {/* File picker + preview */}
        <div className="flex flex-col gap-2">
          <label htmlFor="avatar-file" className="text-xs font-medium text-foreground">
            Choose an image
          </label>
          <input
            id="avatar-file"
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
                alt="Selected picture preview"
                data-testid="avatar-preview"
                className="h-12 w-12 rounded-full border border-border object-cover"
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
            {uploadMutation.isPending ? 'Uploading…' : 'Upload'}
          </button>
          <button
            type="button"
            onClick={handleRemove}
            disabled={saving || (!currentAvatarUrl && !me.isLoading)}
            className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-muted disabled:opacity-50"
          >
            {clearMutation.isPending ? 'Removing…' : 'Remove'}
          </button>
        </div>
      </div>
    </section>
  );
}
