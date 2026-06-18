/**
 * Citation click-through target (AC-2): opens the cited document at the passage.
 * Shows the cited span (charStart–charEnd) and snippet prominently, then embeds
 * the document's original bytes from the api/ boundary content URL
 * (`GET /documents/{id}/content`). The backend enforces permission: a document
 * the caller may not see returns 404 (spec 0004 INV-2), which the iframe surfaces
 * as the server's error page — the UI never fabricates access.
 *
 * Presentational; the parent owns open/close. Renders as a right-side pane.
 */
import { documentContentUrl } from '@/api';
import type { UiCitation } from '../model/citation';

export interface DocumentViewerProps {
  citation: UiCitation;
  onClose: () => void;
}

export function DocumentViewer({ citation, onClose }: DocumentViewerProps) {
  const src = documentContentUrl(citation.documentId);

  return (
    <section
      role="region"
      aria-label={`Cited document: ${citation.documentName}`}
      className="flex h-full min-h-0 flex-col"
    >
      <header className="flex shrink-0 items-start justify-between gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{citation.documentName}</h2>
          <p className="text-[11px] text-foreground-muted">
            Cited passage · characters {citation.charStart}–{citation.charEnd}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close document viewer"
          className="shrink-0 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Close
        </button>
      </header>

      <div className="shrink-0 border-b border-border bg-surface-muted px-3 py-2">
        <p className="text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
          Cited passage
        </p>
        <blockquote className="mt-1 border-l-2 border-accent pl-2 text-sm italic text-foreground">
          {citation.snippet}
        </blockquote>
        <a
          href={src}
          target="_blank"
          rel="noreferrer noopener"
          className="mt-1 inline-block text-xs text-accent underline underline-offset-2"
        >
          Open original in a new tab
        </a>
      </div>

      <div className="min-h-0 flex-1">
        <iframe
          title={`Document: ${citation.documentName}`}
          src={src}
          className="h-full w-full border-0 bg-surface"
        />
      </div>
    </section>
  );
}
