/**
 * DocumentPreviewBody (#242/#245) — the ONE preview body both viewers embed
 * (documents drawer + chat citation viewer), so document rendering behaves
 * identically everywhere.
 *
 * Rendering strategy by type:
 * - Browser-renderable types (pdf / plain text / markdown / images) — fetch the
 *   original bytes through the api/ boundary (`fetchDocumentContent`: bearer
 *   attached, the contract's 302→presigned redirect followed) and render the
 *   resulting `blob:` URL in a sandboxed iframe. The blob carries the stored
 *   content-type, so the browser picks its native viewer.
 * - OOXML office types (docx / pptx / xlsx) — a browser has NO native viewer
 *   (an iframe would show nothing or force a download), so render the server's
 *   extracted text instead (`GET /documents/{id}/text`, #244), labeled as an
 *   extracted-text preview, with a truncation notice when the server capped it.
 * - Every type keeps a "Download original" affordance so the raw bytes are
 *   always one click away (fetched on demand; never auto-downloaded).
 *
 * The office set is decided by the document's `mime_type` when the caller has
 * it (documents feature) and by the fetched blob's content-type when it does
 * not (chat citations — the chat wire carries no mime type).
 *
 * INV-2: a 404 (not permitted / cross-tenant) renders as "no longer available"
 * with no retry — the UI never suggests access might appear. Other failures
 * are actionable (Retry). Object URLs are revoked on unmount/change.
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, fetchDocumentContent, fetchDocumentText } from '@/api';

/** OOXML MIME types a browser cannot render natively (upload allowlist ∩ no-viewer). */
const OFFICE_MIME_TYPES = new Set([
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);

export interface DocumentPreviewBodyProps {
  documentId: string;
  /** Accessible name for the preview frame/region. */
  filename: string;
  /**
   * The document's declared MIME type, when the caller knows it (documents
   * feature). Chat citations don't carry one — leave undefined and the fetched
   * blob's content-type decides the office branch.
   */
  mimeType?: string | undefined;
}

type PreviewState =
  | { kind: 'loading' }
  | { kind: 'frame'; url: string }
  | { kind: 'text'; text: string; truncated: boolean }
  | { kind: 'gone' } // 404 — INV-2: not permitted / deleted; no retry
  | { kind: 'error'; message: string; downloadOnly: boolean };

export function DocumentPreviewBody({ documentId, filename, mimeType }: DocumentPreviewBodyProps) {
  const [state, setState] = useState<PreviewState>({ kind: 'loading' });
  // Bumping retries the load (errors stay actionable, never dead ends).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let revoke: (() => void) | null = null;
    const abort = new AbortController();
    setState({ kind: 'loading' });

    const toState = async (): Promise<PreviewState> => {
      // Known office type: go straight to extracted text (no bytes needed).
      if (mimeType !== undefined && OFFICE_MIME_TYPES.has(mimeType)) {
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      // Otherwise load the bytes; the blob's content-type settles unknown types.
      const content = await fetchDocumentContent(documentId, abort.signal);
      if (OFFICE_MIME_TYPES.has(content.type)) {
        content.revoke(); // office bytes are not rendered — release immediately
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      revoke = content.revoke;
      return { kind: 'frame', url: content.url };
    };

    toState()
      .then((next) => {
        if (cancelled) {
          if (revoke) revoke();
          return;
        }
        setState(next);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ kind: 'gone' });
          return;
        }
        // A text-extraction failure on an office doc still leaves the original
        // bytes downloadable — degrade to that rather than a dead end.
        const officeKnown = mimeType !== undefined && OFFICE_MIME_TYPES.has(mimeType);
        setState({
          kind: 'error',
          message: error instanceof ApiError ? error.displayMessage : 'Could not load the document.',
          downloadOnly: officeKnown,
        });
      });

    return () => {
      cancelled = true;
      abort.abort();
      if (revoke) revoke();
    };
  }, [documentId, mimeType, attempt]);

  const downloadOriginal = useCallback(async () => {
    // Fetched on demand so the office/text path never pays for unused bytes.
    const content = await fetchDocumentContent(documentId);
    const anchor = document.createElement('a');
    anchor.href = content.url;
    anchor.download = filename;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // Revoke on the next tick, not synchronously: some browsers cancel an
    // in-flight download if the object URL is revoked in the same frame as the
    // click. A 0ms defer lets the download pipeline take the blob first.
    setTimeout(content.revoke, 0);
  }, [documentId, filename]);

  if (state.kind === 'loading') {
    return (
      <div role="status" aria-live="polite" className="flex h-full items-center justify-center">
        <span className="text-sm text-foreground-muted">Loading document…</span>
      </div>
    );
  }

  if (state.kind === 'gone') {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6 text-center">
        <p className="text-sm text-foreground-muted">This document is no longer available.</p>
      </div>
    );
  }

  if (state.kind === 'error') {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
      >
        <p className="text-sm font-medium text-danger">Couldn’t open “{filename}”</p>
        <p className="text-sm text-foreground-muted">{state.message}</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setAttempt((n) => n + 1)}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Retry
          </button>
          {state.downloadOnly && (
            <button
              type="button"
              onClick={() => void downloadOriginal()}
              className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Download original
            </button>
          )}
        </div>
      </div>
    );
  }

  if (state.kind === 'text') {
    return (
      <div className="flex h-full flex-col">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
            Extracted text preview
          </span>
          <button
            type="button"
            onClick={() => void downloadOriginal()}
            className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Download original
          </button>
        </div>
        {state.truncated && (
          <p className="shrink-0 border-b border-border bg-surface-muted/40 px-3 py-1 text-[11px] text-foreground-muted">
            Long document — preview truncated. Download the original for the full content.
          </p>
        )}
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-3 font-sans text-sm leading-relaxed text-foreground">
          {state.text}
        </pre>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <iframe
        title={`Preview of ${filename}`}
        src={state.url}
        className="min-h-0 w-full flex-1 border-0 bg-white"
        sandbox=""
      />
      <div className="flex shrink-0 justify-end border-t border-border px-3 py-1.5">
        <button
          type="button"
          onClick={() => void downloadOriginal()}
          className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Download original
        </button>
      </div>
    </div>
  );
}
