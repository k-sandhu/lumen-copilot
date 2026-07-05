/**
 * Server-state hooks for the user settings page — the profile avatar mutations.
 *
 * The avatar's current state rides on `GET /auth/me` (`avatar_url`) — the source both
 * the shell (AccountMenu) and this page read — so on success we invalidate that query,
 * refreshing the shell + page in one step. A 413/415/401 propagates as an `ApiError`
 * the page surfaces; it is NOT swallowed here. The default-model and custom-instructions
 * writes reuse the preferences slice's `useUpdatePreferences` (spec 0005), so they are
 * not re-declared here.
 */
import { useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query';
import { clearAvatar, updateAvatar } from '@/api';
import type { UserAvatar } from '@/api';
import { currentUserQueryKey } from '@/features/auth';

/** Upload the caller's profile avatar; invalidates `/auth/me` so the shell re-reads. */
export function useUpdateAvatar(): UseMutationResult<UserAvatar, unknown, File> {
  const qc = useQueryClient();
  return useMutation<UserAvatar, unknown, File>({
    mutationFn: (file) => updateAvatar(file),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: currentUserQueryKey });
    },
  });
}

/** Clear the caller's profile avatar; invalidates `/auth/me` so the shell re-reads. */
export function useClearAvatar(): UseMutationResult<void, unknown, void> {
  const qc = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: () => clearAvatar(),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: currentUserQueryKey });
    },
  });
}
