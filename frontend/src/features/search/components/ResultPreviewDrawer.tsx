/**
 * ResultPreviewDrawer (#375) — the right-side document preview a search result
 * opens into, so search-to-open is one click instead of a filename hunt through
 * Documents. Embeds the ONE shared `DocumentPreviewBody`: PDF/media through
 * short-lived signed storage URLs, office/text as server-extracted text, and a
 * download-original affordance — identical behavior to the documents drawer and
 * the chat citation viewer. The search wire carries no MIME type, so the body
 * decides by the access capability's MIME type (the chat-citation path).
 *
 * INV-2 stays intact downstream: a document the caller can no longer see 404s
 * and the shared body renders "no longer available" with no retry.
 *
 * A11y mirrors the documents drawer: role="dialog" aria-modal, focus moves to
 * Close on open and is restored on close, Escape and a backdrop click dismiss.
 */
import { useRef } from 'react';
import { DocumentPreviewBody } from '@/components/DocumentPreviewBody';
import { useFocusTrap } from '@/lib/useFocusTrap';

interface ResultPreviewDrawerProps {
  documentId: string;
  /** The result title — the document's filename for corpus results. */
  title: string;
  initialTimeMs?: number;
  onClose: () => void;
}

export function ResultPreviewDrawer({
  documentId,
  title,
  initialTimeMs,
  onClose,
}: ResultPreviewDrawerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  // Mounted only while open, so the trap is always active (open=true).
  useFocusTrap(true, dialogRef, onClose, { initialFocus: closeRef });

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={`Document: ${title}`}
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full w-full max-w-[34rem] flex-col overflow-hidden border-l border-border bg-surface shadow-xl">
        <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{title}</h2>
            <p className="text-[11px] text-foreground-muted">Opened from search results</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close preview"
            className="ml-auto shrink-0 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Close
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="h-full min-h-[24rem] overflow-hidden rounded-md border border-border bg-surface-muted/40">
            <DocumentPreviewBody
              documentId={documentId}
              filename={title}
              {...(initialTimeMs !== undefined ? { initialTimeMs } : {})}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
