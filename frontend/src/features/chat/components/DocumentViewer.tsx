/**
 * Citation click-through target (AC-2): opens the cited document at the passage.
 * Re-skinned (#89) to lead with the kit SourceInspector — the cited passage with
 * the matched span highlighted — then embeds the shared signed preview below
 * it. The SourceInspector makes every answer trace to a verifiable source passage
 * (mission filter #2).
 *
 * SOURCE PROVENANCE (#120 GUARD): media citations can carry player-relative
 * source timestamps and diarized speaker data, which this viewer uses for exact
 * seeking. The chat wire still carries no owner, last-modified, or last-indexed
 * metadata. Its only recency timestamp is the answer/message time, which is the
 * answer's age, not when the source was indexed or modified. The metadata grid
 * therefore renders honest unknowns instead of treating either an answer time or
 * a media offset as indexing freshness.
 *
 * AUTH (INV-4): the API authenticates a JSON access-capability request; the
 * native player/iframe then reads the short-lived storage URL directly. Media
 * citations seek after metadata without autoplay. A fresh visibility denial
 * remains an opaque 404.
 */
import { Icon, SourceInspector } from '@/ui';
import { DocumentPreviewBody } from '@/components/DocumentPreviewBody';
import { cn } from '@/lib/cn';
import { formatMediaTimestamp } from '@/lib/mediaTime';
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
   * "Not available". A media citation's player-relative timestamp is a precise
   * passage location, not indexing recency; do not pass it or the answer time
   * here (#120 GUARD).
   */
  lastIndexed?: string | undefined;
  onClose: () => void;
}

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
            {citation.timeStartMs !== undefined
              ? `Cited at ${formatMediaTimestamp(citation.timeStartMs)}`
              : `Cited passage · characters ${citation.charStart}–${citation.charEnd}`}
            {citation.speakerName ? ` · ${citation.speakerName} (inferred)` : ''}
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
        `freshness` here: answer time is not source indexing recency, and a media
        citation timestamp is only a player-relative passage location. Labelling
        either as freshness would fabricate provenance (#120 GUARD).
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
      </div>

      <div className="min-h-0 flex-1">
        <DocumentPreviewBody
          documentId={documentId}
          filename={citation.documentName}
          {...(citation.timeStartMs !== undefined ? { initialTimeMs: citation.timeStartMs } : {})}
        />
      </div>
    </section>
  );
}
