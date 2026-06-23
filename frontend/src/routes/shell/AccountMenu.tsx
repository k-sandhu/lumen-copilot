/**
 * Account menu (issue #110) — the avatar in the top bar opens a popover with the
 * signed-in principal (from GET /auth/me) and the EXISTING `CurrentUserMenu`
 * (profile + sign out). The avatar shows initials derived from the email; the
 * popover handles loading/error like every async surface (quality bar).
 */
import { CurrentUserMenu, useCurrentUser } from '@/features/auth';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Icon } from '@/ui';
import { useDisclosure } from './useDisclosure';

function initials(email: string | undefined): string {
  if (!email) return '··';
  const name = email.split('@')[0] || email;
  const parts = name.split(/[.\-_]/).filter(Boolean);
  const letters =
    parts.length >= 2 ? `${parts[0]?.[0] ?? ''}${parts[1]?.[0] ?? ''}` : name.slice(0, 2);
  return letters.toUpperCase() || '··';
}

export function AccountMenu() {
  const me = useCurrentUser();
  const { open, toggle, triggerRef, menuRef } = useDisclosure();

  return (
    <div style={{ position: 'relative' }}>
      <button
        ref={triggerRef}
        type="button"
        className="lc-avatar"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        onClick={toggle}
      >
        {me.data ? initials(me.data.email) : <Icon name="user" />}
      </button>

      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Account"
          className="lc-menu"
          style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0 }}
        >
          <div className="lc-menu__head">
            {me.isLoading ? (
              <div className="lc-menu__meta">Loading account…</div>
            ) : me.isError || !me.data ? (
              <div className="lc-menu__error">Account unavailable</div>
            ) : (
              <>
                <div className="lc-menu__name">{me.data.email}</div>
                <div className="lc-menu__meta">
                  Tenant {me.data.tenant_id} · {me.data.roles.join(', ')}
                </div>
              </>
            )}
          </div>
          <div className="lc-menu__sep" />
          <div style={{ padding: '2px 4px 4px' }}>
            <ErrorBoundary label="Account menu">
              <CurrentUserMenu />
            </ErrorBoundary>
          </div>
        </div>
      ) : null}
    </div>
  );
}
