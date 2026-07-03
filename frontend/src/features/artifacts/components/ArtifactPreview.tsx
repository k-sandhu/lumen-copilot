/**
 * ArtifactPreview (#222, AC-2/AC-3) — the inline preview body for one artifact.
 *
 * Rendering strategy by declared MIME type (see model/presentation.previewKind):
 * - `image` (png/jpg/gif/webp) — fetch the bytes through the api/ boundary
 *   (`fetchArtifactContent`: bearer attached, the contract's 302→presigned
 *   redirect followed) and render the resulting `blob:` URL in an `<img>`. svg is
 *   NOT treated as an image (it can carry script) — it falls to the download card.
 * - `text` (plain/markdown/csv/json) — fetch the bytes and render them as
 *   SANITIZED text: markdown through the shared MarkdownView pipeline
 *   (rehype-sanitize), csv/json/plain as an escaped `<pre>`. NEVER raw HTML,
 *   never `dangerouslySetInnerHTML`.
 * - `download` (office/svg/html/unknown) — a download-only card (AC-2). The bytes
 *   are never rendered inline as active content.
 *
 * Every state is handled (frontend/AGENTS.md "every state"): loading, the three
 * populated kinds, a 404 "no longer available" dead-end (INV-2: never suggest
 * access might appear), and an actionable error with Retry. Object URLs are
 * revoked on unmount/change. All previews are size-guarded — a text body past the
 * cap is truncated with an honest notice so a huge artifact can't wedge the pane.
 */
import { useEffect, useState } from 'react';
import { ApiError, fetchArtifactContent } from '@/api';
import { MarkdownView } from '@/lib/markdown';
import { isMarkdown, previewKind } from '../model/presentation';

/** Cap the inline text preview so a large artifact never wedges the pane. */
const TEXT_PREVIEW_MAX_BYTES = 256 * 1024;

export interface ArtifactPreviewProps {
  artifactId: string;
  filename: string;
  /** The artifact's declared MIME type — decides the preview strategy. */
  mimeType: string;
}

type PreviewState =
  | { kind: 'loading' }
  | { kind: 'image'; url: string }
  | { kind: 'text'; text: string; markdown: boolean; truncated: boolean }
  | { kind: 'download' } // previewable inline is not supported for this type
  | { kind: 'gone' } // 404 — INV-2: not permitted / deleted; no retry
  | { kind: 'error'; message: string };

export function ArtifactPreview({ artifactId, filename, mimeType }: ArtifactPreviewProps) {
  const [state, setState] = useState<PreviewState>({ kind: 'loading' });
  // Bumping retries the load (errors stay actionable, never dead ends).
  const [attempt, setAttempt] = useState(0);
  const strategy = previewKind(mimeType);

  useEffect(() => {
    // A download-only type needs no fetch — show the card immediately.
    if (strategy === 'download') {
      setState({ kind: 'download' });
      return;
    }

    let cancelled = false;
    let revoke: (() => void) | null = null;
    const abort = new AbortController();
    setState({ kind: 'loading' });

    const load = async (): Promise<PreviewState> => {
      const content = await fetchArtifactContent(artifactId, abort.signal);
      if (strategy === 'image') {
        revoke = content.revoke;
        return { kind: 'image', url: content.url };
      }
      // Text: read the blob as a string, size-guarded, then release the URL.
      const blob = await fetch(content.url).then((r) => r.blob());
      content.revoke();
      const truncated = blob.size > TEXT_PREVIEW_MAX_BYTES;
      const slice = truncated ? blob.slice(0, TEXT_PREVIEW_MAX_BYTES) : blob;
      const text = await slice.text();
      return { kind: 'text', text, markdown: isMarkdown(mimeType), truncated };
    };

    load()
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
        setState({
          kind: 'error',
          message:
            error instanceof ApiError ? error.displayMessage : 'Could not load this artifact.',
        });
      });

    return () => {
      cancelled = true;
      abort.abort();
      if (revoke) revoke();
    };
  }, [artifactId, mimeType, strategy, attempt]);

  if (state.kind === 'loading') {
    return (
      <div role="status" aria-live="polite" className="flex h-full items-center justify-center p-6">
        <span className="text-sm text-foreground-muted">Loading preview…</span>
      </div>
    );
  }

  if (state.kind === 'download') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-6 text-center">
        <p className="text-sm text-foreground-muted">
          No inline preview for this file type.
        </p>
        <p className="text-xs text-foreground-muted">Use Download to open “{filename}”.</p>
      </div>
    );
  }

  if (state.kind === 'gone') {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6 text-center">
        <p className="text-sm text-foreground-muted">This artifact is no longer available.</p>
      </div>
    );
  }

  if (state.kind === 'error') {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
      >
        <p className="text-sm font-medium text-danger">Couldn’t preview “{filename}”</p>
        <p className="text-sm text-foreground-muted">{state.message}</p>
        <button
          type="button"
          onClick={() => setAttempt((n) => n + 1)}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Retry
        </button>
      </div>
    );
  }

  if (state.kind === 'image') {
    return (
      <div className="flex h-full items-center justify-center overflow-auto bg-surface-muted/30 p-3">
        {/* A blob: image; raster only (svg is routed to download, never inline). */}
        <img
          src={state.url}
          alt={`Preview of ${filename}`}
          className="max-h-full max-w-full object-contain"
        />
      </div>
    );
  }

  // Text (markdown / csv / json / plain) — sanitized, never raw HTML.
  return (
    <div className="flex h-full flex-col">
      {state.truncated && (
        <p className="shrink-0 border-b border-border bg-surface-muted/40 px-3 py-1 text-[11px] text-foreground-muted">
          Large file — preview truncated. Download the original for the full content.
        </p>
      )}
      {state.markdown ? (
        <div className="min-h-0 flex-1 overflow-auto p-3">
          <MarkdownView className="text-sm">{state.text}</MarkdownView>
        </div>
      ) : (
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-foreground">
          {state.text}
        </pre>
      )}
    </div>
  );
}
