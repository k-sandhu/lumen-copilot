/**
 * PermissionPill — the most-repeated trust signal (DESIGN.md §6, mission filter
 * #1 "permissioned by default"). Shows whether the requesting user has access to
 * a source/result: a green "You have access", an amber "Restricted" (masked
 * fields), or a red "No access".
 */
import { cn } from '@/lib/cn';
import { Icon, type IconName } from './Icon';

export type PermissionLevel = 'granted' | 'restricted' | 'denied';

interface PermissionPillProps {
  level: PermissionLevel;
  /** Override the default label (e.g. "Restricted · masked fields"). */
  label?: string;
  /** Detail for assistive tech beyond the visible label. */
  title?: string;
  className?: string;
}

const CONFIG: Record<PermissionLevel, { label: string; icon: IconName; modifier: string }> = {
  granted: { label: 'You have access', icon: 'shield-check', modifier: '' },
  restricted: { label: 'Restricted', icon: 'lock', modifier: 'lc-perm--restricted' },
  denied: { label: 'No access', icon: 'shield-x', modifier: 'lc-perm--denied' },
};

export function PermissionPill({ level, label, title, className }: PermissionPillProps) {
  const cfg = CONFIG[level];
  const text = label ?? cfg.label;
  return (
    <span
      className={cn('lc-perm', cfg.modifier, className)}
      role="status"
      aria-label={title ?? `Permission: ${text}`}
    >
      <Icon name={cfg.icon} />
      {text}
    </span>
  );
}
