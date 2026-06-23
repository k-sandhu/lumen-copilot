/**
 * RiskTierBadge — maps a consequential action to its approval risk tier
 * (DESIGN.md §1 "read before write", §6 "risk-tier badges T0–T3"). T0 auto-runs;
 * T3 needs dual approval. The color escalates with the tier so the cost of an
 * action is legible before it is taken.
 */
import { cn } from '@/lib/cn';

export type RiskTier = 'T0' | 'T1' | 'T2' | 'T3';

interface RiskTierBadgeProps {
  tier: RiskTier;
  /** Show the short policy label after the tier code (default true). */
  showLabel?: boolean;
  /** Override the policy label (e.g. a tenant-specific phrasing). */
  label?: string;
  className?: string;
}

const CONFIG: Record<RiskTier, { label: string; modifier: string }> = {
  T0: { label: 'Auto', modifier: 'lc-badge--ok' },
  T1: { label: 'Notify', modifier: 'lc-badge--info' },
  T2: { label: 'Approval', modifier: 'lc-badge--warn' },
  T3: { label: 'Dual approval', modifier: 'lc-badge--danger' },
};

export function RiskTierBadge({ tier, showLabel = true, label, className }: RiskTierBadgeProps) {
  const cfg = CONFIG[tier];
  const text = label ?? cfg.label;
  return (
    <span
      className={cn('lc-badge', cfg.modifier, className)}
      aria-label={`Risk tier ${tier}: ${text}`}
    >
      <span className="lc-mono">{tier}</span>
      {showLabel ? <span>{text}</span> : null}
    </span>
  );
}
