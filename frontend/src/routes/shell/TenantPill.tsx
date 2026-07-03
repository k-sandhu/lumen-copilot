/**
 * Tenant pill (issue #110) — shows the active tenant from the principal the app
 * already loads (GET /auth/me, via `useCurrentUser`) with a "hard isolation"
 * tooltip (spec 0004 INV-1: tenant data never crosses the boundary). Reuses the
 * shared Radix Tooltip mounted at the app root. Handles loading/error like every
 * async surface (quality bar) so the chrome never shows a blank or stuck pill.
 */
import { useCurrentUser } from '@/features/auth';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/Tooltip';

export function TenantPill() {
  const me = useCurrentUser();

  // Show the human-readable tenant name (#247); the raw id stays in the tooltip
  // for support/debugging. Loading/error keep an honest placeholder.
  const label = me.isLoading
    ? 'Loading tenant…'
    : me.isError || !me.data
      ? 'Tenant unavailable'
      : me.data.tenant_name;
  const idHint = me.data ? `Tenant ID: ${me.data.tenant_id}` : undefined;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="lc-pillbtn"
          data-testid="tenant-pill"
          tabIndex={0}
          aria-live="polite"
          title={idHint}
        >
          <span className="lc-tenant__dot" aria-hidden="true" />
          <span className="lc-tenant__name">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        Active tenant — hard isolation. Your workspace is sealed from other tenants.
        {idHint ? ` (${idHint})` : ''}
      </TooltipContent>
    </Tooltip>
  );
}
