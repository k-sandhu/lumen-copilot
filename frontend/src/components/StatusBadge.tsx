import * as Tooltip from '@radix-ui/react-tooltip';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

export type StatusTone = 'ok' | 'degraded' | 'pending' | 'danger';

const toneStyles: Record<StatusTone, string> = {
  ok: 'bg-ok/15 text-ok',
  degraded: 'bg-warn/15 text-warn',
  pending: 'bg-foreground-muted/15 text-foreground-muted',
  danger: 'bg-danger/15 text-danger',
};

const dotStyles: Record<StatusTone, string> = {
  ok: 'bg-ok',
  degraded: 'bg-warn',
  pending: 'bg-foreground-muted',
  danger: 'bg-danger',
};

interface StatusBadgeProps {
  tone: StatusTone;
  children: ReactNode;
  /** Optional tooltip detail (e.g. a dependency's failure detail). */
  detail?: string;
  /** Animate the dot (e.g. pending/connecting). Honors prefers-reduced-motion via CSS. */
  pulse?: boolean;
}

/** Accessible status pill with a colored dot and optional tooltip detail. */
export function StatusBadge({ tone, children, detail, pulse = false }: StatusBadgeProps) {
  const badge = (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium',
        toneStyles[tone],
      )}
    >
      <span
        aria-hidden="true"
        className={cn('h-1.5 w-1.5 rounded-full', dotStyles[tone], pulse && 'animate-pulse')}
      />
      {children}
    </span>
  );

  if (!detail) return badge;

  return (
    <Tooltip.Provider delayDuration={150}>
      <Tooltip.Root>
        {/* asChild keeps the trigger semantics on the badge itself. */}
        <Tooltip.Trigger asChild>
          <button
            type="button"
            className="cursor-help rounded-full focus-visible:ring-2 focus-visible:ring-accent"
            aria-label={`Status detail: ${detail}`}
          >
            {badge}
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            sideOffset={6}
            className="z-50 max-w-xs rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground shadow-md"
          >
            {detail}
            <Tooltip.Arrow className="fill-border" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
