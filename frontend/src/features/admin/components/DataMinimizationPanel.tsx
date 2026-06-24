/**
 * DataMinimizationPanel — the read-only Data-minimization surface (#122,
 * admin.html). The wireframe shows interactive policy toggles (index DMs, index
 * email, exclude Confidential, …). Two hard constraints apply:
 *
 *   1. The admin console is READ-ONLY for v1 (ADR-0007 §4) — no mutating
 *      controls, so no toggles regardless.
 *   2. The frozen contract (contracts/openapi.yaml §admin) exposes NO
 *      data-minimization policy endpoint, and neither spec 0003 nor spec 0004
 *      defines or enforces tenant-level data-minimization defaults (which
 *      sources are excluded, what is audited). Per ADR-0007 decision 5, this is
 *      wireframe-implied behavior that must be confirmed before implementation.
 *
 * So this panel makes NO governance or privacy promises the backend cannot
 * prove. It does not claim sources are excluded by default or that changes are
 * audited — none of that is specified or enforced yet. It simply states that the
 * data-minimization policy surface is not yet exposed and will appear here, read
 * only, once the contract + backend behavior land. No invented data, no dead
 * toggles, no unprovable claims.
 */
import { Icon } from '@/ui';

export function DataMinimizationPanel() {
  return (
    <section
      aria-labelledby="admin-datamin-heading"
      className="rounded-lg border border-border"
    >
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-datamin-heading" className="text-sm font-semibold text-foreground">
          Data minimization
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Tenant data-minimization policy is not yet exposed by the backend.
        </p>
      </header>

      <div className="p-4">
        <div
          role="note"
          className="rounded-md border border-border/60 bg-surface-muted/40 p-4 text-sm text-foreground-muted"
        >
          <p className="flex items-start gap-2">
            <Icon name="lock" aria-hidden="true" className="mt-0.5 text-foreground" />
            <span>
              The data-minimization policy controls (which sources are indexed, what is
              excluded) are a <span className="text-foreground">write</span> surface that is
              not yet defined in the contract or served by the backend. They are coming soon.
            </span>
          </p>
          <p className="mt-3 pl-7">
            When the policy API lands, this panel will display the tenant&rsquo;s current policy
            read-only here. Until then there is nothing to fabricate — no policy data is served,
            so none is shown.
          </p>
        </div>
      </div>
    </section>
  );
}
