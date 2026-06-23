/**
 * Citation click-through target (AC-2): opens the cited document at the passage.
 * Re-skinned (#89) to lead with the kit SourceInspector — the cited passage with
 * the matched span highlighted, plus freshness — then embeds the document's
 * original bytes below it. The SourceInspector makes every answer trace to a
 * verifiable source passage (mission filter #2).
 *
 * AUTH (INV-4): `GET /documents/{id}/content` is a `bearerAuth` endpoint — it is
 * authorized by the in-memory access JWT, not a cookie. A browser-initiated
 * `<iframe src>` / `<a href>` GET cannot attach an `Authorization: Bearer`
 * header, so we cannot point them at the bare endpoint (it would 401). Instead
 * we fetch the bytes through the api/ boundary (`fetchDocumentContent`, bearer
 * attached, the contract's optional 302→presigned redirect followed) and render
 * the resulting `blob:` object URL. The backend enforces permission: a document
 * the caller may not see returns 404 (spec 0004 INV-2), surfaced here as an
 * actionable error — the UI never fabricates access. The object URL is revoked
 * on unmount / when the citation changes.
 */
import { useEffect, useState } from 'react';
import { fetchDocumentContent } from '@/api';
import { SourceInspector } from '@/ui';
import type { UiCitation } from '../model/citation';
import { passageFromCitation } from '../model/presentation';

export interface DocumentViewerProps {
  citation: UiCitation;
  /** Optional freshness label for the cited source (e.g. "2d ago"). */
  freshness?: string | undefined;
  /** Mark the source as past its freshness window. */
  stale?: boolean | undefined;
  onClose: () => void;
}

type ContentState =
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'error'; message: string };

export function DocumentViewer({ citation, freshness, stale, onClose }: DocumentViewerProps) {
  const { documentId } = citation;
  const [content, setContent] = useState<ContentState>({ status: 'loading' });
  // Bumping this retries the load (AC-2: errors are actionable, not dead ends).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let revoke: (() => void) | null = null;
    const abort = new AbortController();
    setContent({ status: 'loading' });

    fetchDocumentContent(documentId, abort.signal)
      .then((result) => {
        if (cancelled) {
          // Lost the race (unmount / citation change): release immediately.
          result.revoke();
          return;
        }
        revoke = result.revoke;
        setContent({ status: 'ready', url: result.url });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message =
          error instanceof Error ? error.message : 'Could not load the document.';
        setContent({ status: 'error', message });
      });

    return () => {
      cancelled = true;
      abort.abort();
      if (revoke) revoke();
    };
  }, [documentId, attempt]);

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

      {/* The kit SourceInspector surfaces the cited passage + freshness. */}
      <div className="shrink-0 border-b border-border px-3 py-2">
        <SourceInspector
          title={citation.documentName}
          passage={passageFromCitation(citation)}
          {...(freshness ? { freshness } : {})}
          {...(stale !== undefined ? { stale } : {})}
        />
        {content.status === 'ready' && (
          <a
            href={content.url}
            target="_blank"
            rel="noreferrer noopener"
            className="mt-1 inline-block text-xs text-accent underline underline-offset-2"
          >
            Open original in a new tab
          </a>
        )}
      </div>

      <div className="min-h-0 flex-1">
        {content.status === 'loading' && (
          <div
            role="status"
            aria-live="polite"
            className="flex h-full items-center justify-center p-4 text-sm text-foreground-muted"
          >
            Loading document…
          </div>
        )}

        {content.status === 'error' && (
          <div
            role="alert"
            className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center text-sm"
          >
            <p className="text-foreground">Could not open this document.</p>
            <p className="text-[11px] text-foreground-muted">{content.message}</p>
            <button
              type="button"
              onClick={() => setAttempt((n) => n + 1)}
              className="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Retry
            </button>
          </div>
        )}

        {content.status === 'ready' && (
          <iframe
            title={`Document: ${citation.documentName}`}
            src={content.url}
            sandbox=""
            className="h-full w-full border-0 bg-surface"
          />
        )}
      </div>
    </section>
  );
}
