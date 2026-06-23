/**
 * StatusDot — a small colored dot + optional label used for sync/health state
 * (DESIGN.md §6 "status / sync dots"). The `sync` tone pulses to signal live
 * activity; the pulse is purely a CSS animation, so `prefers-reduced-motion`
 * (honored in ui-kit.css) collapses it automatically.
 */
import { cn } from '@/lib/cn';

export type StatusTone = 'ok' | 'warn' | 'danger' | 'muted' | 'sync';

interface StatusDotProps {
  tone: StatusTone;
  /** Optional visible label next to the dot. */
  label?: string;
  /**
   * Accessible name. Defaults to the label; required (here or via label) when
   * the dot stands alone so screen-reader users get the state.
   */
  title?: string;
  className?: string;
}

const TONE_CLASS: Record<StatusTone, string> = {
  ok: 'lc-dot--ok',
  warn: 'lc-dot--warn',
  danger: 'lc-dot--danger',
  muted: 'lc-dot--muted',
  sync: 'lc-dot--sync',
};

export function StatusDot({ tone, label, title, className }: StatusDotProps) {
  const accessibleName = title ?? label;
  return (
    <span className={cn('lc-status', className)} role="status" aria-label={accessibleName}>
      <span className={cn('lc-dot', TONE_CLASS[tone])} aria-hidden="true" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}
