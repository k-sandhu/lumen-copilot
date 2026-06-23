/**
 * TrimNotice (#84) — the permission-trim disclosure. When the backend withholds
 * results the caller may not see, it returns a `hidden_count` (never the content
 * itself; spec 0004 INV-2). This surfaces that as "N results hidden — you don't
 * have access" so the trim is honest and visible, without leaking what was hidden.
 *
 * Renders nothing when nothing was hidden.
 */
import { Icon } from '@/ui';
import { trimNotice } from '../model/presentation';

interface TrimNoticeProps {
  hiddenCount: number;
}

export function TrimNotice({ hiddenCount }: TrimNoticeProps) {
  const message = trimNotice(hiddenCount);
  if (message === null) return null;

  return (
    <div
      role="note"
      aria-label="Permission trim notice"
      className="flex items-center gap-2 rounded-md border border-border bg-surface-muted px-3 py-2 text-xs text-foreground-muted"
    >
      <Icon name="lock" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
