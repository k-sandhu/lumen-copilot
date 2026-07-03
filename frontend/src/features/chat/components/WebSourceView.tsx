/**
 * Web-citation click-through target (#221, epic E3-12). A web citation resolves
 * to a PUBLIC web page — not a corpus document — so it never goes through the
 * document-bytes viewer (which fetches `GET /documents/{id}/content` and would
 * 404 for a web result that has no `document_id`, INV-3). Instead this pane shows
 * the cited snippet via the kit SourceInspector in its web variant (globe +
 * host), and an external-link affordance that opens the page in a new tab with
 * `rel="noopener noreferrer"` — the safe, distinct rendering AC-2 requires.
 *
 * A URL we cannot classify as http(s) (a malformed or non-web scheme) never
 * produces an outbound link — `isSafeHttpUrl` gates it, so a web citation can
 * never yield an unsafe/dead link. All states are covered: a citation with no
 * snippet still renders its title/host and the link (never a blank pane).
 */
import { Icon, SourceInspector } from '@/ui';
import type { UiCitation } from '../model/citation';
import { hostOf, isSafeHttpUrl } from '../model/citation';
import { passageFromCitation } from '../model/presentation';

export interface WebSourceViewProps {
  citation: UiCitation;
  onClose: () => void;
}

export function WebSourceView({ citation, onClose }: WebSourceViewProps) {
  const host = hostOf(citation.url);
  const safeHref = isSafeHttpUrl(citation.url) ? citation.url : undefined;
  const title = citation.webTitle ?? citation.documentName ?? host ?? 'Web result';
  const hasSnippet = citation.snippet.trim().length > 0;

  return (
    <section role="region" aria-label={`Cited web page: ${title}`} className="lc-viewer">
      <header className="lc-viewer__head">
        <div className="min-w-0">
          <h2 className="lc-viewer__title">{title}</h2>
          <p className="lc-viewer__sub">
            <Icon name="globe" /> {host ?? 'Web source'}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close web source"
          className="lc-iconbtn shrink-0"
        >
          <Icon name="x" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {/* No PermissionPill: a public web page isn't a permissioned corpus
            source, so a "You have access" pill would misstate provenance. The
            web variant (globe + host) is the honest trust signal here. */}
        <SourceInspector
          title={title}
          {...(hasSnippet ? { passage: passageFromCitation(citation) } : {})}
          {...(host ? { web: { host } } : {})}
          {...(safeHref ? { href: safeHref } : {})}
        />

        {!hasSnippet ? (
          <p className="mt-2 text-xs italic text-foreground-muted">
            No excerpt was captured for this page — open it to read the source.
          </p>
        ) : null}
      </div>
    </section>
  );
}
