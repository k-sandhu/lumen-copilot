/**
 * DocumentPreviewBody (#242/#245) — the ONE preview body both viewers embed
 * (documents drawer + chat citation viewer), so document rendering behaves
 * identically everywhere.
 *
 * Rendering strategy by type:
 * - PDF — the browser's one dependable native viewer. Fetch the original bytes
 *   through the api/ boundary (`fetchDocumentContent`: bearer attached, the
 *   contract's 302→presigned redirect followed) and render the resulting `blob:`
 *   URL in an iframe. The iframe is left UNsandboxed: a fully-restrictive
 *   sandbox="" blocks Chrome's out-of-process PDF viewer (broken-plugin
 *   placeholder). Safe — the upload allowlist admits no active-content types
 *   (no HTML/SVG/scripts), so a PDF frame carries no script vector.
 * - Everything else (office docx/pptx/xlsx AND plain text / markdown) — render
 *   the server's reassembled text (`GET /documents/{id}/text`, #244), labeled as
 *   an extracted-text preview, with a truncation notice when the server capped
 *   it. A blob iframe shows office types as nothing/a download and renders
 *   text/* blank under any restrictive sandbox, so the text endpoint is the
 *   reliable, consistently-styled path for all of them.
 * - Every type keeps a "Download original" affordance so the raw bytes are
 *   always one click away (fetched on demand; never auto-downloaded).
 *
 * The type is decided by the document's `mime_type` when the caller has it
 * (documents feature) and by the fetched blob's content-type when it does not
 * (chat citations — the chat wire carries no mime type).
 *
 * INV-2: a 404 (not permitted / cross-tenant) renders as "no longer available"
 * with no retry — the UI never suggests access might appear. Other failures
 * are actionable (Retry). Object URLs are revoked on unmount/change.
 */
import { useCallback, useEffect, useState } from 'react';
import { ApiError, fetchDocumentContent, fetchDocumentText } from '@/api';

/** OOXML office MIME types a browser cannot render natively. */
const OFFICE_MIME_TYPES = new Set([
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);

/**
 * Types previewed as server-extracted text (via `GET /documents/{id}/text`, #244)
 * rather than in an iframe: the office set PLUS plain text / markdown. A blob
 * iframe shows office types as nothing/a download, and — under any restrictive
 * sandbox — renders text/* blank in Chrome. Routing all of them through the
 * reassembled-text endpoint is reliable and consistently styled. Only PDF (the
 * browser's one dependable native viewer) stays an iframe.
 */
const TEXT_PREVIEW_MIME_TYPES = new Set([...OFFICE_MIME_TYPES, 'text/plain', 'text/markdown']);

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
  // Only PDFs use the iframe now (text types render via /text); it stays unsandboxed.
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
      // Known text-extractable type (office / plain text / markdown): render the
      // server's reassembled text — no bytes needed, reliable and styled.
      if (mimeType !== undefined && TEXT_PREVIEW_MIME_TYPES.has(mimeType)) {
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      // Otherwise load the bytes; the blob's content-type settles unknown types
      // (chat citations carry no declared mime type).
      const content = await fetchDocumentContent(documentId, abort.signal);
      if (TEXT_PREVIEW_MIME_TYPES.has(content.type)) {
        content.revoke(); // text is served via /text — release the bytes now
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      // Only PDFs reach here (the sole type with a dependable browser viewer).
      // The blob iframe is left UNsandboxed — a restrictive sandbox="" blocks
      // Chrome's out-of-process PDF viewer (broken-plugin placeholder). Safe:
      // the upload allowlist admits no active content (no HTML/SVG/scripts).
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
        // A text-extraction failure still leaves the original bytes downloadable
        // — degrade to that rather than a dead end (office + text/markdown).
        const textTypeKnown = mimeType !== undefined && TEXT_PREVIEW_MIME_TYPES.has(mimeType);
        setState({
          kind: 'error',
          message: error instanceof ApiError ? error.displayMessage : 'Could not load the document.',
          downloadOnly: textTypeKnown,
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
      {/* PDF-only frame, intentionally UNsandboxed: a restrictive sandbox=""
          blocks Chrome's out-of-process PDF viewer (broken-plugin placeholder).
          Safe — the upload allowlist admits no active content (no HTML/SVG/
          scripts); every other type renders via the /text path, not here. */}
      <iframe
        title={`Preview of ${filename}`}
        src={state.url}
        className="min-h-0 w-full flex-1 border-0 bg-white"
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
