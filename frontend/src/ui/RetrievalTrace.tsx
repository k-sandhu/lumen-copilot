/**
 * RetrievalTrace — a collapsible record of what an answer searched, ranked, and
 * excluded (DESIGN.md §1, §6 "retrieval trace"). It makes the grounding legible:
 * "Looked at 3 sources · 1,204 passages · 38 excluded". Excluded steps (e.g. a
 * permission-trimmed candidate) render muted so the trim is visible, not hidden.
 *
 * Self-contained disclosure: an accessible <button> with aria-expanded toggles
 * the step list. Loading and empty states are handled.
 */
import { useId, useState } from 'react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export interface TraceStep {
  /** What happened, e.g. "Searched Drive · 412 passages". */
  label: string;
  /** Render this step as an exclusion (muted, dot icon) vs. an inclusion (check). */
  excluded?: boolean;
}

interface RetrievalTraceProps {
  /** One-line summary shown in the header, e.g. "3 sources · 1,204 passages · 38 excluded". */
  summary: string;
  steps?: TraceStep[];
  /** Start expanded (default collapsed). */
  defaultOpen?: boolean;
  loading?: boolean;
  className?: string;
}

export function RetrievalTrace({
  summary,
  steps = [],
  defaultOpen = false,
  loading = false,
  className,
}: RetrievalTraceProps) {
  const [open, setOpen] = useState(defaultOpen);
  const listId = useId();
  const hasSteps = steps.length > 0;

  return (
    <div className={cn('lc-trace', className)}>
      <button
        type="button"
        className="lc-trace__head"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((v) => !v)}
        disabled={loading}
      >
        <Icon name="search" />
        <span>Retrieval</span>
        <span className="lc-trace__summary">{summary}</span>
        <Icon name="chevron-down" className="lc-trace__chevron" />
      </button>
      {open ? (
        <div id={listId} role="region" aria-label="Retrieval steps">
          {loading ? (
            <div className="lc-trace__step" aria-busy="true">
              <span className="lc-skeleton" style={{ width: '70%' }} />
            </div>
          ) : !hasSteps ? (
            <div className="lc-trace__step">No retrieval steps recorded.</div>
          ) : (
            steps.map((step, i) => (
              <div
                key={i}
                className={cn('lc-trace__step', step.excluded && 'lc-trace__step--excluded')}
              >
                <Icon name={step.excluded ? 'minus' : 'check'} />
                {step.label}
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
