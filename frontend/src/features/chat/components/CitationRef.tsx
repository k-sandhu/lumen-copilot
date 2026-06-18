/**
 * A single clickable, passage-level citation reference (AC-2, spec 0004 INV-3).
 * Renders as a small numbered chip; clicking opens the document viewer at the
 * cited span (`documentId` + `charStart/charEnd`). Keyboard-activatable (it is a
 * real <button>) with an accessible label naming the source document.
 */
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/Tooltip';
import { cn } from '@/lib/cn';

export interface CitationRefProps {
  /** 1-based display index within the message. */
  index: number;
  documentName: string;
  snippet: string;
  onOpen: () => void;
}

export function CitationRef({ index, documentName, snippet, onOpen }: CitationRefProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onOpen}
          aria-label={`Citation ${index}: open ${documentName} at the cited passage`}
          className={cn(
            'mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded px-1 align-super',
            'text-[10px] font-semibold leading-none',
            'bg-accent/15 text-accent hover:bg-accent/25',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
        >
          {index}
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <span className="block max-w-xs">
          <span className="font-medium">{documentName}</span>
          <span className="mt-0.5 block text-foreground-muted">
            {snippet.length > 140 ? `${snippet.slice(0, 140)}…` : snippet}
          </span>
        </span>
      </TooltipContent>
    </Tooltip>
  );
}
