/**
 * ModelGovernancePanel — the read-only Model-governance view (#88/#122, ADR-0007
 * §4): which models are allowed for this tenant and the governance tier each maps
 * to. Rendered as a styled READ-ONLY table (allowed models + tiers). There are NO
 * mutating controls — and specifically NONE of the wireframe's enable / default
 * switches: an admin views governance here but does not edit it in v1
 * (read-before-write, mission filter #3). Admin-only; 403/401 render via PanelBody
 * as actionable errors.
 *
 * Columns are limited to what the contract serves. The frozen `ModelGovernance`
 * shape carries `allowed_models` ({ model_id, tier, label? }) and tier
 * descriptions — so the wireframe's "Capability" and "Speed & cost" columns are
 * OMITTED (no backing data; AGENTS.md scope guard: never fake fields). Every
 * allowed model is shown even when its tier isn't declared in `tiers` (never drop
 * a model the backend reports as allowed).
 */
import { useMemo } from 'react';
import type { ModelGovernance, ModelGovernanceEntry } from '@/api';
import { useModelGovernance } from '../model/queries';
import { PanelBody } from './PanelState';

interface GovernanceRow extends ModelGovernanceEntry {
  /** The tier's human description, when the contract declared one. */
  tierDescription?: string;
}

/** Flatten allowed models into table rows, attaching each tier's description when
 *  the contract declared one. Order: declared-tier order first (so the table
 *  reads tier-by-tier), then any models on a tier not present in `tiers` — never
 *  dropping a model the backend reports as allowed. */
function toRows(governance: ModelGovernance): GovernanceRow[] {
  const description = new Map(governance.tiers.map((t) => [t.id, t.description]));
  const order = new Map(governance.tiers.map((t, i) => [t.id, i]));
  const undeclared = governance.tiers.length; // sorts after every declared tier

  return governance.allowed_models
    .map((model) => ({ ...model, tierDescription: description.get(model.tier) }))
    .sort((a, b) => (order.get(a.tier) ?? undeclared) - (order.get(b.tier) ?? undeclared));
}

function GovernanceTable({ rows }: { rows: GovernanceRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Models allowed for this tenant and their governance tier</caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Model
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Model ID
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Governance tier
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.model_id} className="border-b border-border/60 last:border-0">
              <td className="px-4 py-3 align-middle font-medium text-foreground">
                {row.label ?? row.model_id}
              </td>
              <td className="px-4 py-3 align-middle">
                {/* Show the raw wire id only when a friendlier label exists —
                    otherwise the name already IS the id (don't print it twice). */}
                {row.label ? (
                  <span className="lc-mono text-xs text-foreground-muted">{row.model_id}</span>
                ) : (
                  <span className="text-foreground-muted">—</span>
                )}
              </td>
              <td className="px-4 py-3 align-middle">
                <span
                  className="lc-mono rounded bg-surface-muted px-1.5 py-0.5 text-xs uppercase text-foreground"
                  title={row.tierDescription}
                >
                  {row.tier}
                </span>
                {row.tierDescription ? (
                  <span className="ml-2 text-xs text-foreground-muted">{row.tierDescription}</span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ModelGovernancePanel() {
  const query = useModelGovernance();
  const rows = useMemo(() => (query.data ? toRows(query.data) : []), [query.data]);
  const isEmpty =
    (query.data?.allowed_models.length ?? 0) === 0 && (query.data?.tiers.length ?? 0) === 0;

  return (
    <section aria-labelledby="admin-governance-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-governance-heading" className="text-sm font-semibold text-foreground">
          Model governance
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Read-only view of the models allowed for this tenant and the governance tier each maps to.
          All model calls route through the gateway.
        </p>
      </header>
      <PanelBody
        label="model governance"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={isEmpty}
        emptyMessage="No model governance is configured for this tenant."
        onRetry={() => void query.refetch()}
        loadingRows={3}
      >
        <GovernanceTable rows={rows} />
      </PanelBody>
    </section>
  );
}
