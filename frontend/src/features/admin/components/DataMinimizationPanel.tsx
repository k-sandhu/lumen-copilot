/**
 * DataMinimizationPanel — the read-only Data-minimization surface (#122,
 * admin.html). The wireframe shows interactive policy toggles (index DMs, index
 * email, exclude Confidential, …). Two hard constraints apply:
 *
 *   1. The admin console is READ-ONLY for v1 (ADR-0007 §4) — no mutating
 *      controls, so no toggles regardless.
 *   2. The frozen contract (contracts/openapi.yaml §admin) exposes NO
 *      data-minimization policy endpoint. Per ADR-0007 decision 5 and AGENTS.md
 *      scope guard, "data-minimization toggles" are flagged as behavior NOT yet
 *      in spec 0003/0004 and confirmed-before-build — we must not invent the
 *      policy values to render switches against.
 *
 * So this panel does NOT fake a policy list. It states the data-minimization
 * principle the product commits to (index only what's needed; off by default;
 * every exclusion audited — spec 0003/0004) and is explicit that the per-policy
 * controls are a deferred write surface, not yet served by the backend. This
 * keeps the tab honest: no invented data, no dead toggles.
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
          Index only what&rsquo;s needed. Sources are off by default; every exclusion change is
          recorded in the audit log.
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
              The product&rsquo;s data-minimization stance — index only what&rsquo;s needed, private
              channels and sensitivity-labelled content excluded by default, and every change
              audited — is a core commitment (spec 0003/0004). The per-policy controls that
              configure it are a <span className="text-foreground">write</span> surface, gated
              behind read-before-write, and are not yet exposed by the backend.
            </span>
          </p>
          <p className="mt-3 pl-7">
            When the policy API lands, this panel will display the tenant&rsquo;s current exclusions
            read-only here. Until then there is nothing to fabricate — no policy data is served, so
            none is shown.
          </p>
        </div>
      </div>
    </section>
  );
}
