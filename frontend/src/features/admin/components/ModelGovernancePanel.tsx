/**
 * ModelGovernancePanel — the read-only Model-governance view (#88, ADR-0007 §4):
 * which models are allowed for this tenant and the governance tier each maps to.
 * No mutating controls — an admin views governance here but does not edit it in
 * v1 (read-before-write, mission filter #3). Admin-only; 403/401 render via
 * PanelBody as actionable errors.
 *
 * Models are grouped under their tier so the policy is legible: each tier shows
 * its description (when known) and the allowed models beneath it. A model whose
 * tier has no declared description still renders under its tier id.
 */
import { useMemo } from 'react';
import type { ModelGovernance, ModelGovernanceEntry } from '@/api';
import { useModelGovernance } from '../model/queries';
import { PanelBody } from './PanelState';

interface TierGroup {
  id: string;
  description?: string;
  models: ModelGovernanceEntry[];
}

/** Group allowed models by tier, preserving the contract's tier order first,
 *  then appending any tier referenced by a model but absent from `tiers`. */
function groupByTier(governance: ModelGovernance): TierGroup[] {
  const byTier = new Map<string, ModelGovernanceEntry[]>();
  for (const model of governance.allowed_models) {
    const bucket = byTier.get(model.tier) ?? [];
    bucket.push(model);
    byTier.set(model.tier, bucket);
  }

  const ordered: TierGroup[] = [];
  const seen = new Set<string>();
  for (const tier of governance.tiers) {
    ordered.push({ id: tier.id, description: tier.description, models: byTier.get(tier.id) ?? [] });
    seen.add(tier.id);
  }
  // Tiers referenced by a model but not declared in `tiers` — never drop a model.
  for (const tier of byTier.keys()) {
    if (!seen.has(tier)) {
      ordered.push({ id: tier, models: byTier.get(tier) ?? [] });
    }
  }
  return ordered;
}

function TierBlock({ group }: { group: TierGroup }) {
  return (
    <div className="rounded-md border border-border/60 p-3">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">
          <span className="lc-mono rounded bg-surface-muted px-1.5 py-0.5 text-xs uppercase">
            {group.id}
          </span>
        </h3>
        <span className="text-xs text-foreground-muted">
          {group.models.length} model{group.models.length === 1 ? '' : 's'}
        </span>
      </div>
      {group.description ? (
        <p className="mt-1 text-xs text-foreground-muted">{group.description}</p>
      ) : null}
      {group.models.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {group.models.map((model) => (
            <li key={model.model_id} className="flex items-baseline justify-between gap-3 text-sm">
              <span className="font-medium text-foreground">{model.label ?? model.model_id}</span>
              {/* Show the raw wire id only when a friendlier label is present —
                  otherwise the name already IS the id (don't print it twice). */}
              {model.label ? (
                <span className="lc-mono truncate text-xs text-foreground-muted">
                  {model.model_id}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-foreground-muted">No models approved at this tier.</p>
      )}
    </div>
  );
}

export function ModelGovernancePanel() {
  const query = useModelGovernance();
  const groups = useMemo(() => (query.data ? groupByTier(query.data) : []), [query.data]);
  const isEmpty =
    (query.data?.allowed_models.length ?? 0) === 0 && (query.data?.tiers.length ?? 0) === 0;

  return (
    <section aria-labelledby="admin-governance-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-governance-heading" className="text-sm font-semibold text-foreground">
          Model governance
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Read-only view of the models allowed for this tenant, grouped by governance tier.
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
        <div className="grid gap-3 p-4 sm:grid-cols-2">
          {groups.map((group) => (
            <TierBlock key={group.id} group={group} />
          ))}
        </div>
      </PanelBody>
    </section>
  );
}
