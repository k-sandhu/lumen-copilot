/**
 * Document viewer (#49 AC-3, re-skinned for #89) — a right-side drawer that
 * surfaces the document's trust signals before its bytes: a metadata grid, the
 * parse → chunk → embed → ready INGESTION TRACE (so a user can see exactly how a
 * file became answerable), and — when opened on a cited passage — the cited
 * SourceInspector passage. It then resolves the original file via
 * `GET /documents/{id}/content` (following a 302 to a short-TTL presigned URL)
 * and renders it in an isolated sandboxed iframe.
 *
 * A11y: role="dialog" aria-modal, focus moves into the drawer on open and is
 * restored on close, Escape and a backdrop click dismiss. Loading / error /
 * success states are all handled; a not-permitted document (INV-2 → 404) shows a
 * clear message.
 */
import { useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ApiError, resolveDocumentContentUrl } from '@/api';
import type { Document } from '@/api';
import { SourceInspector, StatusDot, type SourcePassage } from '@/ui';
import { useFocusTrap } from '@/lib/useFocusTrap';
import { formatBytes, fileKind, ingestSteps, type IngestStep } from '../model/presentation';

interface DocumentViewerProps {
  doc: Document;
  /** When opened from a citation, the cited passage to surface (#89). */
  citedPassage?: SourcePassage;
  onClose: () => void;
}

const INGEST_TONE: Record<IngestStep['state'], 'ok' | 'sync' | 'muted' | 'danger'> = {
  done: 'ok',
  active: 'sync',
  pending: 'muted',
  failed: 'danger',
};

export function DocumentViewer({ doc, citedPassage, onClose }: DocumentViewerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const query = useQuery<string>({
    queryKey: ['document-content', doc.id],
    queryFn: ({ signal }) => resolveDocumentContentUrl(doc.id, signal),
    // Presigned URLs are short-TTL; never cache the resolved URL.
    staleTime: 0,
    gcTime: 0,
    retry: false,
    // Only the iframe preview needs the bytes; a failed-ingest doc has none.
    enabled: doc.status === 'ready',
  });

  // Move focus to the Close button on open, trap Tab inside the drawer, restore
  // focus on close; Escape dismisses. The component is mounted only while open,
  // so the trap is always active (open=true).
  useFocusTrap(true, dialogRef, onClose, { initialFocus: closeRef });

  const steps = ingestSteps(doc);

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={`Document: ${doc.filename}`}
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full w-full max-w-[34rem] flex-col overflow-hidden border-l border-border bg-surface shadow-xl">
        <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
          <span className="rounded bg-surface-muted px-1.5 py-0.5 text-[10px] font-semibold text-foreground-muted">
            {fileKind(doc)}
          </span>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{doc.filename}</h2>
            <p className="text-[11px] text-foreground-muted">
              {fileKind(doc)} · {formatBytes(doc.size_bytes)}
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close viewer"
            className="ml-auto shrink-0 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Close
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* Metadata grid */}
          <dl className="grid grid-cols-2 gap-3 border-b border-border p-4 text-sm">
            <Meta label="Type" value={fileKind(doc)} />
            <Meta label="Size" value={formatBytes(doc.size_bytes)} />
            <Meta label="Chunks" value={doc.status === 'ready' ? String(doc.chunk_count) : '—'} />
            <Meta label="Status" value={<StatusLine doc={doc} />} />
          </dl>

          {/* Ingestion trace (parse → chunk → embed → ready) */}
          <section aria-label="Ingestion" className="border-b border-border p-4">
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
              Ingestion
            </p>
            <ul className="flex flex-col gap-1.5">
              {steps.map((step) => (
                <li key={step.key} className="flex items-center gap-2 text-sm">
                  <StatusDot
                    tone={INGEST_TONE[step.state]}
                    title={`${step.label}: ${step.state}`}
                  />
                  <span
                    className={
                      step.state === 'pending'
                        ? 'text-foreground-muted'
                        : step.state === 'failed'
                          ? 'text-danger'
                          : 'text-foreground'
                    }
                  >
                    {step.label}
                  </span>
                </li>
              ))}
            </ul>
            {doc.status === 'failed' && doc.error && (
              <p role="alert" className="mt-2 rounded bg-danger/10 px-2 py-1 text-xs text-danger">
                {doc.error}
              </p>
            )}
          </section>

          {/* Cited passage, when opened from a citation */}
          {citedPassage && (
            <section aria-label="Cited passage" className="border-b border-border p-4">
              <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
                Cited passage
              </p>
              <SourceInspector title={doc.filename} passage={citedPassage} />
            </section>
          )}

          {/* Preview (bytes in a sandboxed iframe) */}
          <section aria-label="Preview" className="p-4">
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
              Preview
            </p>
            <div className="h-[28rem] overflow-hidden rounded-md border border-border bg-surface-muted/40">
              <ViewerBody doc={doc} query={query} />
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-foreground-muted">{label}</dt>
      <dd className="mt-0.5 text-sm">{value}</dd>
    </div>
  );
}

function StatusLine({ doc }: { doc: Document }) {
  const tone =
    doc.status === 'ready'
      ? ('ok' as const)
      : doc.status === 'failed'
        ? ('danger' as const)
        : doc.status === 'processing'
          ? ('sync' as const)
          : ('muted' as const);
  const label =
    doc.status === 'ready'
      ? 'Indexed'
      : doc.status === 'failed'
        ? 'Failed'
        : doc.status === 'processing'
          ? 'Processing…'
          : 'Queued';
  return <StatusDot tone={tone} label={label} />;
}

type ContentQuery = ReturnType<typeof useQuery<string>>;

function ViewerBody({ doc, query }: { doc: Document; query: ContentQuery }) {
  if (doc.status !== 'ready') {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center">
        <span className="text-sm text-foreground-muted">
          Preview is available once ingestion completes.
        </span>
      </div>
    );
  }

  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" className="flex h-full items-center justify-center">
        <span className="text-sm text-foreground-muted">Loading document…</span>
      </div>
    );
  }

  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    const message = notFound
      ? 'This document is no longer available.'
      : query.error instanceof ApiError
        ? query.error.displayMessage
        : 'Could not load the document.';
    return (
      <div role="alert" className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-sm font-medium text-danger">Couldn’t open “{doc.filename}”</p>
        <p className="text-sm text-foreground-muted">{message}</p>
        {!notFound && (
          <button
            type="button"
            onClick={() => void query.refetch()}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  // Success — render the bytes in a sandboxed iframe (browser picks the right
  // viewer for PDFs / images / text). `sandbox` (no allow-scripts) isolates
  // untrusted document content from the app.
  return (
    <iframe
      title={`Preview of ${doc.filename}`}
      src={query.data}
      className="h-full w-full border-0 bg-white"
      sandbox=""
    />
  );
}
