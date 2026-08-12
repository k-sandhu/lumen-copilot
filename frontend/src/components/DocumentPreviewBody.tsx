/**
 * DocumentPreviewBody (#242/#245) — the ONE preview body both viewers embed
 * (documents drawer + chat citation viewer), so document rendering behaves
 * identically everywhere.
 *
 * Rendering strategy by type:
 * - PDF — mint a short-lived JSON access capability and render its storage URL
 *   directly in an iframe. The iframe is left UNsandboxed: a fully-restrictive
 *   sandbox="" blocks Chrome's out-of-process PDF viewer (broken-plugin
 *   placeholder). Safe — the upload allowlist admits no active-content types
 *   (no HTML/SVG/scripts), so a PDF frame carries no script vector.
 * - Audio/video — native metadata-only playback over a signed Range URL plus a
 *   paginated timestamped transcript.
 * - Office docx/pptx/xlsx and plain text/markdown — render
 *   the server's reassembled text (`GET /documents/{id}/text`, #244), labeled as
 *   an extracted-text preview, with a truncation notice when the server capped
 *   it. A browser iframe shows office types as nothing/a download and renders
 *   text/* blank under any restrictive sandbox, so the text endpoint is the
 *   reliable, consistently-styled path for all of them.
 * - Every type keeps a purpose-bound signed "Download original" affordance.
 *
 * The type is decided by the document's `mime_type` when the caller has it
 * (documents feature) and by the access capability's MIME type otherwise.
 *
 * INV-2: a 404 (not permitted / cross-tenant) renders as "no longer available"
 * with no retry — the UI never suggests access might appear. Other failures
 * are actionable (Retry).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  createDocumentAccessUrl,
  fetchDocumentText,
  type DocumentAccessUrl,
} from '@/api';
import { MediaTranscriptPlayer } from './MediaTranscriptPlayer';

/** OOXML office MIME types a browser cannot render natively. */
const OFFICE_MIME_TYPES = new Set([
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);

/**
 * Types previewed as server-extracted text (via `GET /documents/{id}/text`, #244)
 * rather than in an iframe: the office set PLUS plain text / markdown. An iframe
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
   * feature). Chat citations may omit it; the access capability decides.
   */
  mimeType?: string | undefined;
  /** Media citation target; seeks after metadata loads and never autoplays. */
  initialTimeMs?: number | undefined;
}

type PreviewState =
  | { kind: 'loading' }
  // Only PDFs use the iframe now (text types render via /text); it stays unsandboxed.
  | { kind: 'frame'; url: string }
  | { kind: 'media'; mediaKind: 'audio' | 'video'; access: DocumentAccessUrl }
  | { kind: 'text'; text: string; truncated: boolean }
  | { kind: 'processing' }
  | { kind: 'gone' } // 404 — INV-2: not permitted / deleted; no retry
  | { kind: 'error'; message: string; downloadOnly: boolean };

export function DocumentPreviewBody({
  documentId,
  filename,
  mimeType,
  initialTimeMs,
}: DocumentPreviewBodyProps) {
  const [state, setState] = useState<PreviewState>({ kind: 'loading' });
  const [downloadError, setDownloadError] = useState<string | null>(null);
  // Bumping retries the load (errors stay actionable, never dead ends).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const abort = new AbortController();
    setState({ kind: 'loading' });

    const toState = async (): Promise<PreviewState> => {
      // Known text-extractable type (office / plain text / markdown): render the
      // server's reassembled text — no bytes needed, reliable and styled.
      if (mimeType !== undefined && TEXT_PREVIEW_MIME_TYPES.has(mimeType)) {
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      // Mint a JSON access capability; browser bytes then flow directly from
      // storage (including native media Range requests).
      const access = await createDocumentAccessUrl(documentId, 'preview', abort.signal);
      if (TEXT_PREVIEW_MIME_TYPES.has(access.mime_type)) {
        const text = await fetchDocumentText(documentId, abort.signal);
        return { kind: 'text', text: text.text, truncated: text.truncated };
      }
      if (isAudioMime(access.mime_type)) {
        return { kind: 'media', mediaKind: 'audio', access };
      }
      if (isVideoMime(access.mime_type)) {
        return { kind: 'media', mediaKind: 'video', access };
      }
      // Only PDFs reach here (the sole type with a dependable browser viewer).
      // The signed-URL iframe is left UNsandboxed — a restrictive sandbox="" blocks
      // Chrome's out-of-process PDF viewer (broken-plugin placeholder). Safe:
      // the upload allowlist admits no active content (no HTML/SVG/scripts).
      return { kind: 'frame', url: access.url };
    };

    toState()
      .then((next) => {
        if (cancelled) return;
        setState(next);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ kind: 'gone' });
          return;
        }
        if (error instanceof ApiError && error.status === 409) {
          setState({ kind: 'processing' });
          return;
        }
        // A text-extraction failure still leaves the original bytes downloadable
        // — degrade to that rather than a dead end (office + text/markdown).
        const textTypeKnown = mimeType !== undefined && TEXT_PREVIEW_MIME_TYPES.has(mimeType);
        setState({
          kind: 'error',
          message:
            error instanceof ApiError ? error.displayMessage : 'Could not load the document.',
          downloadOnly: textTypeKnown,
        });
      });

    return () => {
      cancelled = true;
      abort.abort();
    };
  }, [documentId, mimeType, attempt]);

  const downloadOriginal = useCallback(async () => {
    setDownloadError(null);
    try {
      const access = await createDocumentAccessUrl(documentId, 'download');
      const anchor = document.createElement('a');
      anchor.href = access.url;
      anchor.download = access.filename || filename;
      anchor.rel = 'noopener';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch (error) {
      setDownloadError(
        error instanceof ApiError ? error.displayMessage : 'Could not start the download.',
      );
    }
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

  if (state.kind === 'processing') {
    return (
      <div
        role="status"
        className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
      >
        <p className="text-sm text-foreground-muted">This file is still being processed.</p>
        <button
          type="button"
          onClick={() => setAttempt((value) => value + 1)}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted"
        >
          Check again
        </button>
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
        {downloadError ? (
          <p role="alert" className="shrink-0 border-b border-border px-3 py-1 text-xs text-danger">
            {downloadError}
          </p>
        ) : null}
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-3 font-sans text-sm leading-relaxed text-foreground">
          {state.text}
        </pre>
      </div>
    );
  }

  if (state.kind === 'media') {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="min-h-0 flex-1">
          <MediaTranscriptPlayer
            documentId={documentId}
            filename={filename}
            kind={state.mediaKind}
            initialAccess={state.access}
            {...(initialTimeMs !== undefined ? { initialTimeMs } : {})}
          />
        </div>
        <div className="flex shrink-0 justify-end border-t border-border px-3 py-1.5">
          <button
            type="button"
            onClick={() => void downloadOriginal()}
            className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Download original
          </button>
        </div>
        {downloadError ? (
          <p role="alert" className="shrink-0 px-3 py-1 text-xs text-danger">
            {downloadError}
          </p>
        ) : null}
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
      {downloadError ? (
        <p role="alert" className="shrink-0 px-3 py-1 text-xs text-danger">
          {downloadError}
        </p>
      ) : null}
    </div>
  );
}

function isAudioMime(mime: string): boolean {
  return mime.startsWith('audio/');
}

function isVideoMime(mime: string): boolean {
  return mime.startsWith('video/');
}
