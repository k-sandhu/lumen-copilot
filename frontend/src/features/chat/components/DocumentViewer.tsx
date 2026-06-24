/**
 * Citation click-through target (AC-2): opens the cited document at the passage.
 * Re-skinned (#89) to lead with the kit SourceInspector — the cited passage with
 * the matched span highlighted — then embeds the document's original bytes below
 * it. The SourceInspector makes every answer trace to a verifiable source passage
 * (mission filter #2).
 *
 * SOURCE PROVENANCE (#120 GUARD): the chat/citation wire (Citation /
 * ChatCitation) carries NO source-provenance fields — no owner, no last-modified,
 * no last-indexed timestamp. The only timestamp a chat turn has is the
 * answer/message time, which is the answer's age, NOT when the source was indexed
 * or modified. So the metadata grid (owner / last-modified / last-indexed) and
 * the inspector freshness pill render "Not available" / nothing rather than
 * present the answer time as source provenance — never fabricate where a doc was
 * last indexed. A field lights up honestly only when a source actually carries
 * it (e.g. the `owner` prop, wired through if a source ever starts providing one).
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
import { Icon, SourceInspector } from '@/ui';
import { cn } from '@/lib/cn';
import type { UiCitation } from '../model/citation';
import { passageFromCitation, sourceMetadataRows } from '../model/presentation';

export interface DocumentViewerProps {
  citation: UiCitation;
  /**
   * Source owner for the metadata grid (#120), when known. The chat/citation
   * wire doesn't carry it today, so the grid shows "Not available" rather than
   * fabricate a name — this prop lets it light up honestly once a source has one.
   */
  owner?: string | undefined;
  /**
   * Last-modified label for the metadata grid (#120), when known. Not on the
   * chat wire today → "Not available"; wired so a source can light it up.
   */
  lastModified?: string | undefined;
  /**
   * Last-indexed label for the metadata grid (#120), when known. Not on the
   * chat wire today (the citation contract carries no indexing timestamp) →
   * "Not available". Do NOT pass the answer/message time here: that is when the
   * answer was produced, not when the source was indexed (#120 GUARD).
   */
  lastIndexed?: string | undefined;
  onClose: () => void;
}

type ContentState =
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'error'; message: string };

export function DocumentViewer({
  citation,
  owner,
  lastModified,
  lastIndexed,
  onClose,
}: DocumentViewerProps) {
  const { documentId } = citation;
  // The inspector metadata grid (#120). NONE of owner / last-modified /
  // last-indexed are on the chat/citation wire — and the answer/message time is
  // the answer's age, not source provenance — so each renders "Not available"
  // unless a source actually carries it. We never present the answer time as a
  // source-indexing timestamp (#120 GUARD against fabricated provenance).
  const metadataRows = sourceMetadataRows({ owner, lastModified, lastIndexed });
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
      className="lc-viewer"
    >
      <header className="lc-viewer__head">
        <div className="min-w-0">
          <h2 className="lc-viewer__title">{citation.documentName}</h2>
          <p className="lc-viewer__sub">
            Cited passage · characters {citation.charStart}–{citation.charEnd}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close document viewer"
          className="lc-iconbtn shrink-0"
        >
          <Icon name="x" />
        </button>
      </header>

      {/*
        The kit SourceInspector surfaces the cited passage. We do NOT pass a
        `freshness` here: the only timestamp a chat turn carries is the answer
        time, and labelling that as source freshness/indexing would fabricate
        provenance (#120 GUARD). Owner lights up only if a source carries one.
      */}
      {/*
        Cap the passage/metadata block and let it scroll: a long cited passage
        must never squeeze the document iframe to zero or clip the "Open original"
        link against the shell's overflow:hidden (the inspector pane itself doesn't
        scroll). The iframe region below keeps the remaining space.
      */}
      <div className="max-h-[45%] shrink-0 overflow-y-auto border-b border-border px-3 py-2">
        <SourceInspector
          title={citation.documentName}
          passage={passageFromCitation(citation)}
          {...(owner ? { owner } : {})}
        />

        {/* Source-inspector metadata grid (#120): owner / last-modified / last-indexed. */}
        <dl
          role="group"
          aria-label="Source metadata"
          className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs"
        >
          {metadataRows.map((row) => (
            <div key={row.label} className="contents">
              <dt className="text-foreground-muted">{row.label}</dt>
              <dd
                className={cn(
                  'min-w-0 truncate text-right',
                  row.unknown ? 'italic text-foreground-muted' : 'text-foreground',
                )}
              >
                {row.value}
              </dd>
            </div>
          ))}
        </dl>

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
