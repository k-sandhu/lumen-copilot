/**
 * Document viewer (#49 AC-3) — resolves the original file via
 * `GET /documents/{id}/content`, following a 302 to a short-TTL presigned URL,
 * and renders it in an isolated sandboxed iframe. Modeled as a modal overlay
 * with managed focus and Escape-to-close. Loading / error / success states are
 * all handled; a not-permitted document (INV-2 → 404) shows a clear message.
 *
 * NOTE (in-document citation highlight is explicitly OUT of scope for #49 — it
 * lands with the chat citations UI).
 */
import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ApiError, resolveDocumentContentUrl } from '@/api';
import type { Document } from '@/api';

interface DocumentViewerProps {
  doc: Document;
  onClose: () => void;
}

export function DocumentViewer({ doc, onClose }: DocumentViewerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);

  const query = useQuery<string>({
    queryKey: ['document-content', doc.id],
    queryFn: ({ signal }) => resolveDocumentContentUrl(doc.id, signal),
    // Presigned URLs are short-TTL; never cache the resolved URL.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

  // Manage focus + Escape-to-close for the modal.
  useEffect(() => {
    closeRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Document: ${doc.filename}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-xl">
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h2 className="truncate text-sm font-semibold">{doc.filename}</h2>
          <div className="flex items-center gap-2">
            {query.isSuccess && (
              <a
                href={query.data}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted"
              >
                Open in new tab
              </a>
            )}
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              aria-label="Close viewer"
              className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted"
            >
              Close
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 bg-surface-muted/40">
          <ViewerBody doc={doc} query={query} />
        </div>
      </div>
    </div>
  );
}

type ContentQuery = ReturnType<typeof useQuery<string>>;

function ViewerBody({ doc, query }: { doc: Document; query: ContentQuery }) {
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
