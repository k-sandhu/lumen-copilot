/**
 * AdminHeader — the tenant-scoped header for the read-only admin console (#122,
 * admin.html: "Admin · <tenant> · governance, models, and data controls"). The
 * tenant identity is the REAL principal the app already loads (GET /auth/me, via
 * useCurrentUser) — the backend exposes a `tenant_id` (a UUID), not a human
 * display name, so we show the id honestly rather than inventing a company name
 * (AGENTS.md scope guard: never fake backend-unsupported data). Tenant isolation
 * is a hard invariant (spec 0004 INV-1).
 *
 * Like every async surface (frontend/AGENTS.md), it never shows a blank or stuck
 * value: loading and unavailable both resolve to legible text.
 */
import { useCurrentUser } from '@/features/auth';

export function AdminHeader() {
  const me = useCurrentUser();

  const tenant = me.isLoading
    ? 'loading tenant…'
    : me.isError || !me.data
      ? 'tenant unavailable'
      : me.data.tenant_id;

  return (
    <header>
      <h1 className="text-lg font-semibold text-foreground">Admin</h1>
      <p className="mt-1 text-sm text-foreground-muted">
        <span aria-live="polite">
          Tenant <span className="lc-mono text-foreground">{tenant}</span>
        </span>{' '}
        &middot; governance, models, and data controls.
      </p>
      <p className="mt-2 text-xs text-foreground-muted">
        Read-only for v1: viewing members, model policy, approval tiers, and data-minimization
        policy. Editing them is gated behind read-before-write controls and is not available here.
      </p>
    </header>
  );
}
