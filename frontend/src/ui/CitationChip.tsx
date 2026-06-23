/**
 * CitationChip — the inline `[n]` marker that makes an answer citation-backed
 * (DESIGN.md §1 "citation-backed", §6 "citation chip + source inspector"). It is
 * a real button: clicking it opens the matching SourceInspector. `active`
 * reflects the open/selected state via `aria-pressed`, which the kit CSS styles.
 */
import { cn } from '@/lib/cn';

interface CitationChipProps {
  /** 1-based citation number rendered inside the chip. */
  index: number;
  /** Whether this chip's source is currently open in the inspector. */
  active?: boolean;
  onClick?: () => void;
  /** Source title for the accessible name (e.g. "Q3 Revenue.pdf"). */
  sourceTitle?: string;
  className?: string;
}

export function CitationChip({
  index,
  active = false,
  onClick,
  sourceTitle,
  className,
}: CitationChipProps) {
  const label = sourceTitle ? `Citation ${index}: ${sourceTitle}` : `Citation ${index}`;
  return (
    <button
      type="button"
      className={cn('lc-cite', className)}
      aria-pressed={active}
      aria-label={label}
      onClick={onClick}
    >
      {index}
    </button>
  );
}
