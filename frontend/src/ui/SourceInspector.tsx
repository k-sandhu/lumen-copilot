/**
 * SourceInspector — the passage panel a CitationChip opens (DESIGN.md §6). It
 * shows the exact cited passage with the matched span `<mark>`-highlighted, plus
 * owner / freshness / visibility and an "Open in source" deep link, so every
 * answer traces to a verifiable source passage (mission filter #2).
 *
 * Implements every state: `loading` (skeleton), `error` (actionable retry),
 * empty (no source selected), and success.
 */
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';
import { FreshnessPill } from './FreshnessPill';
import { PermissionPill, type PermissionLevel } from './PermissionPill';

export interface SourcePassage {
  /** The passage text, split into runs; runs with `highlight` render in <mark>. */
  runs: Array<{ text: string; highlight?: boolean }>;
}

export interface SourceInspectorProps {
  /** Source title (file/page/thread name). */
  title?: string;
  /** The cited passage. */
  passage?: SourcePassage;
  /** Document/source owner, e.g. "Dana Ruiz". */
  owner?: string;
  /** Freshness label, e.g. "Indexed 2h ago". */
  freshness?: string;
  /** Mark freshness as stale (amber). */
  stale?: boolean;
  /** The requesting user's permission on this source. */
  permission?: PermissionLevel;
  /** Deep-link target for "Open in source". */
  href?: string;
  /** Called when the panel is dismissed (renders a close button when set). */
  onClose?: () => void;
  loading?: boolean;
  /** Error message; when set, renders the error state with an optional retry. */
  error?: string;
  onRetry?: () => void;
  className?: string;
}

function PassageBody({ passage }: { passage: SourcePassage }): ReactNode {
  return (
    <p className="lc-passage">
      {passage.runs.map((run, i) =>
        run.highlight ? <mark key={i}>{run.text}</mark> : <span key={i}>{run.text}</span>,
      )}
    </p>
  );
}

export function SourceInspector({
  title,
  passage,
  owner,
  freshness,
  stale = false,
  permission,
  href,
  onClose,
  loading = false,
  error,
  onRetry,
  className,
}: SourceInspectorProps) {
  const hasSource = Boolean(title || passage);

  return (
    <section
      className={cn('lc-inspector', className)}
      aria-label={title ? `Source: ${title}` : 'Source inspector'}
    >
      <header className="lc-inspector__head">
        <Icon name="file-text" />
        <span className="lc-inspector__title">{title ?? 'Source'}</span>
        {onClose ? (
          <button
            type="button"
            className="lc-iconbtn"
            style={{ marginLeft: 'auto' }}
            aria-label="Close source inspector"
            onClick={onClose}
          >
            <Icon name="x" />
          </button>
        ) : null}
      </header>

      <div className="lc-inspector__body">
        {loading ? (
          <div aria-busy="true" aria-label="Loading source">
            <div className="lc-skeleton" style={{ width: '100%' }} />
            <div className="lc-skeleton" style={{ width: '92%', marginTop: '8px' }} />
            <div className="lc-skeleton" style={{ width: '78%', marginTop: '8px' }} />
          </div>
        ) : error ? (
          <div className="lc-state lc-state--error" role="alert">
            <Icon name="alert-triangle" />
            <span>{error}</span>
            {onRetry ? (
              <button type="button" className="lc-link" onClick={onRetry}>
                Try again
              </button>
            ) : null}
          </div>
        ) : !hasSource ? (
          <div className="lc-state">
            <Icon name="file-text" />
            <span>Select a citation to view its source.</span>
          </div>
        ) : (
          <>
            {passage ? <PassageBody passage={passage} /> : null}
            <div className="lc-inspector__meta">
              {owner ? (
                <span className="lc-meta-item">
                  <Icon name="user" />
                  {owner}
                </span>
              ) : null}
              {freshness ? <FreshnessPill label={freshness} stale={stale} /> : null}
              {permission ? <PermissionPill level={permission} /> : null}
            </div>
            {href ? (
              <a className="lc-link" href={href} target="_blank" rel="noreferrer noopener">
                Open in source
                <Icon name="arrow-up-right" />
              </a>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
