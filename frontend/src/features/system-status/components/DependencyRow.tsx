import { StatusBadge } from '@/components/StatusBadge';
import type { DependencyStatus } from '@/api';

/** One dependency line: name + ok/degraded badge, with detail in a tooltip. */
export function DependencyRow({ dep }: { dep: DependencyStatus }) {
  return (
    <li className="flex items-center justify-between gap-3 border-b border-border py-2 last:border-b-0">
      <span className="truncate font-mono text-sm">{dep.name}</span>
      <StatusBadge tone={dep.ok ? 'ok' : 'degraded'} detail={dep.detail}>
        {dep.ok ? 'ok' : 'degraded'}
      </StatusBadge>
    </li>
  );
}
