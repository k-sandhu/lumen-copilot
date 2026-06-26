/**
 * ProvenanceDrawer — the proof surface (DESIGN.md §1 "auditable", §6). Opened
 * from an AuditRow, it shows the event metadata, the per-candidate allow/exclude
 * decisions (including the EXCLUDED candidates, so a permission trim is provable
 * after the fact, mission filter #4), and the raw event payload.
 *
 * A11y: rendered as role="dialog" aria-modal; focus moves into the drawer on
 * open and returns to the opener on close; Escape and a backdrop click dismiss.
 * Motion is CSS-only and collapses under prefers-reduced-motion.
 */
import { useId, useRef } from 'react';
import { cn } from '@/lib/cn';
import { useFocusTrap } from '@/lib/useFocusTrap';
import { Icon } from './Icon';

export interface ProvenanceCandidate {
  /** Candidate source/passage title. */
  title: string;
  /** Whether this candidate was allowed into the answer or excluded. */
  decision: 'allowed' | 'excluded';
  /** Why it was excluded/allowed, e.g. "no access" or "rank 1". */
  reason?: string;
}

export interface ProvenanceDetail {
  /** Event id for the drawer title. */
  id: string;
  /** Ordered key/value metadata (actor, tenant, model, latency…). */
  meta?: Array<{ key: string; value: string }>;
  /** The allow/exclude candidate ledger. */
  candidates?: ProvenanceCandidate[];
  /** Raw event payload, pretty-printed by the caller or by `raw`. */
  raw?: unknown;
}

interface ProvenanceDrawerProps {
  open: boolean;
  detail?: ProvenanceDetail;
  onClose: () => void;
}

export function ProvenanceDrawer({ open, detail, onClose }: ProvenanceDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  // Move focus into the drawer on open, trap Tab inside it, restore focus on
  // close; Escape dismisses. Shared with every other aria-modal surface.
  useFocusTrap(open, panelRef, onClose, { initialFocus: panelRef });

  if (!open) return null;

  const rawText =
    detail?.raw === undefined
      ? null
      : typeof detail.raw === 'string'
        ? detail.raw
        : JSON.stringify(detail.raw, null, 2);

  return (
    <>
      <button
        type="button"
        className="lc-backdrop"
        aria-label="Close provenance drawer"
        tabIndex={-1}
        onClick={onClose}
      />
      <div
        className="lc-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="lc-drawer__head">
          <Icon name="file-text" />
          <span className="lc-drawer__title" id={titleId}>
            Provenance{detail ? ` · ${detail.id}` : ''}
          </span>
          <button
            type="button"
            className="lc-iconbtn"
            style={{ marginLeft: 'auto' }}
            aria-label="Close"
            onClick={onClose}
          >
            <Icon name="x" />
          </button>
        </header>

        <div className="lc-drawer__body">
          {detail?.meta && detail.meta.length > 0 ? (
            <dl className="lc-kv">
              {detail.meta.map((m) => (
                <div key={m.key} style={{ display: 'contents' }}>
                  <dt>{m.key}</dt>
                  <dd>{m.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {detail?.candidates && detail.candidates.length > 0 ? (
            <section aria-label="Candidate decisions">
              <div className="lc-kpi__k" style={{ marginBottom: 'calc(6px * var(--space))' }}>
                Candidates
              </div>
              {detail.candidates.map((c, i) => (
                <div
                  key={i}
                  className={cn('lc-cand')}
                  style={{ marginBottom: 'calc(6px * var(--space))' }}
                >
                  <span
                    className={cn(
                      'lc-dot',
                      c.decision === 'allowed' ? 'lc-dot--ok' : 'lc-dot--muted',
                    )}
                    aria-hidden="true"
                  />
                  <span className="lc-cand__title">{c.title}</span>
                  <span style={{ color: 'var(--text-subtle)' }}>{c.reason ?? c.decision}</span>
                </div>
              ))}
            </section>
          ) : null}

          {rawText !== null ? (
            <section aria-label="Raw event payload">
              <div className="lc-kpi__k" style={{ marginBottom: 'calc(6px * var(--space))' }}>
                Raw event
              </div>
              <pre className="lc-pre">{rawText}</pre>
            </section>
          ) : null}

          {!detail ? (
            <div className="lc-state">
              <Icon name="file-text" />
              <span>No event selected.</span>
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}
