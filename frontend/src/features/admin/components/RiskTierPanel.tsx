/**
 * RiskTierPanel — the read-only Approvals & risk-tier map (#88, ADR-0007 §4):
 * the read-before-write action tiers T0–T3 (spec 0004 §2.5), each with what it
 * means and the approval it requires. Uses the design-system RiskTierBadge from
 * @/ui so the colour escalates with the tier (T0 auto → T3 dual approval),
 * keeping the cost of an action legible before it is taken.
 *
 * This is a REFERENCE map, not a control surface — there are no approve/deny
 * actions here; the screen is read-mostly for v1. The contract's RiskTierId
 * (T0–T3) maps 1:1 onto the kit's RiskTier prop.
 */
import { RiskTierBadge, type RiskTier as KitRiskTier } from '@/ui';
import type { RiskTier, RiskTierId } from '@/api';
import { useRiskTiers } from '../model/queries';
import { PanelBody } from './PanelState';

/** The contract's RiskTierId is a structural subset of the kit's RiskTier; this
 *  narrows the wire id onto the badge prop without an unsafe cast. */
function toBadgeTier(id: RiskTierId): KitRiskTier {
  return id;
}

function TierRow({ tier }: { tier: RiskTier }) {
  return (
    <tr className="border-b border-border/60 last:border-0 align-top">
      <td className="px-4 py-3">
        <RiskTierBadge tier={toBadgeTier(tier.tier)} />
      </td>
      <td className="px-4 py-3 text-sm text-foreground">{tier.description}</td>
      <td className="px-4 py-3 text-sm text-foreground-muted">{tier.approval}</td>
    </tr>
  );
}

function RiskTierTable({ tiers }: { tiers: RiskTier[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Read-before-write risk tiers T0 to T3</caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Tier
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              What it covers
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Approval required
            </th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((tier) => (
            <TierRow key={tier.tier} tier={tier} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RiskTierPanel() {
  const query = useRiskTiers();
  const tiers = query.data?.items ?? [];

  return (
    <section aria-labelledby="admin-risk-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-risk-heading" className="text-sm font-semibold text-foreground">
          Approvals &amp; risk tiers
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          The read-before-write tiers (T0–T3): each action&rsquo;s risk level and the approval it
          requires.
        </p>
      </header>
      <PanelBody
        label="risk tiers"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={tiers.length === 0}
        emptyMessage="No risk tiers are configured."
        onRetry={() => void query.refetch()}
        loadingRows={4}
      >
        <RiskTierTable tiers={tiers} />
      </PanelBody>
    </section>
  );
}
