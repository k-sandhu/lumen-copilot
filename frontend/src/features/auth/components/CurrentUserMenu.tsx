/**
 * Current-user menu for the app-shell header (AC-2). Renders the signed-in
 * principal from GET /auth/me and a sign-out control that calls POST
 * /auth/logout and clears state. Handles loading / error like every async
 * surface (quality bar) so the header never shows a blank or stuck state.
 */
import { useCurrentUser, useLogout } from '../model/queries';

export function CurrentUserMenu() {
  const me = useCurrentUser();
  const logout = useLogout();

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-foreground-muted" aria-live="polite">
        {me.isLoading && 'Loading account…'}
        {me.isError && 'Account unavailable'}
        {me.data && (
          <span title={`Tenant ${me.data.tenant_id} · ${me.data.roles.join(', ')}`}>
            {me.data.email}
          </span>
        )}
      </span>
      <button
        type="button"
        onClick={() => logout.mutate()}
        disabled={logout.isPending}
        className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted disabled:opacity-60"
      >
        {logout.isPending ? 'Signing out…' : 'Sign out'}
      </button>
    </div>
  );
}
