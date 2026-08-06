/**
 * Citations arrive in two shapes that mean the same thing:
 *   - REST `Citation` (snake_case) — persisted, from GET .../messages.
 *   - WS `ChatCitation` (camelCase) — live, from event:citation.
 * Both mirror spec 0004 INV-3. This normalizes them to one UI shape so the
 * renderer and the viewer treat live and reloaded citations identically.
 *
 * Web knowledge mode (#221, epic E3-12): the merged `web_search` tool (#219)
 * cites a public web page — a URL + snippet, and NEVER a corpus `document_id`
 * (a document_id would imply the content is in the permissioned corpus). The
 * frozen citation wire (`Citation` / `ChatCitation`) does not YET carry a `url`
 * or a `sourceType` discriminator, so we accept it additively here and, until
 * the contract types it, DISTINGUISH a web citation by URL presence (a
 * heuristic). See `kindOfCitation` + the TODO below.
 *
 * TODO(#221 follow-up): once the contract types a web citation distinctly
 * (a `sourceType: 'web' | 'document'` discriminator + a `url` on the citation
 * envelope — the backend already models this internally, see
 * backend/app/services/tools/impls/web_search.py `_payload`), replace the
 * URL-presence heuristic in `kindOfCitation` with the wire discriminator and
 * drop `WebCitationExtras` from the hand-parse in `streamReducer`/here.
 */
import type { ChatCitation, Citation } from '@/api';

/** Whether a citation resolves to a corpus document or a public web page. */
export type CitationKind = 'document' | 'web';

/** Unified citation the UI renders + the viewer opens on. */
export interface UiCitation {
  id: string;
  documentId: string;
  documentName: string;
  chunkId: string;
  snippet: string;
  charStart: number;
  charEnd: number;
  score?: number;
  /**
   * Present only for a web citation (#221): the cited page URL. Not on the
   * frozen wire yet — carried additively so the renderer can show host/link and
   * so `kindOfCitation` can classify. Absent ⇒ a corpus document citation.
   */
  url?: string;
  /**
   * Present only for a web citation: the page title (falls back to the host).
   * `documentName` is reused for the primary label either way.
   */
  webTitle?: string;
  /**
   * The reader has lost access to the cited document since the answer was written
   * (#536), so the server withheld the passage and the filename. Render the
   * reference as unavailable rather than dropping it — the claim was genuinely
   * grounded, just not in something this reader may still see.
   */
  redacted?: boolean;
}

/**
 * The additive web fields a citation payload MAY carry ahead of the contract
 * typing them (see TODO in the file header). Kept narrow so the hand-parse in
 * the stream reducer stays honest about what it reads.
 */
export interface WebCitationExtras {
  url?: unknown;
  webTitle?: unknown;
}

/**
 * Classify a citation as a web page vs a corpus document. Heuristic until the
 * contract carries a discriminator (#221 follow-up): a citation with a SAFE
 * http(s) `url` ⇒ web. A citation with no url — or a url that isn't safe http(s)
 * (a `javascript:`/`data:` scheme, a malformed string) — is treated as a corpus
 * document, so a hostile URL can never be promoted into a web citation with an
 * outbound link. This keeps the "distinct web rendering" path safe by default.
 */
export function kindOfCitation(c: Pick<UiCitation, 'url'>): CitationKind {
  return isSafeHttpUrl(c.url) ? 'web' : 'document';
}

/**
 * The registrable, human-readable host of a URL, e.g.
 * "https://en.wikipedia.org/wiki/X?y=1" → "en.wikipedia.org". Returns null for a
 * URL we cannot parse or one that isn't http(s), so callers omit the host chip
 * rather than render a bogus one. `www.` is stripped for a cleaner label.
 */
export function hostOf(url: string | undefined): string | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return parsed.hostname.replace(/^www\./, '') || null;
}

/**
 * True only for an http(s) URL — the gate for rendering an external link.
 * Everything else (a `javascript:`/`data:` URL, a malformed string) is refused
 * so a web citation never produces an unsafe or dead outbound link.
 */
export function isSafeHttpUrl(url: string | undefined): url is string {
  return hostOf(url) !== null;
}

/** Pull the additive web fields off a raw citation object, if present + valid. */
function webExtras(extras: WebCitationExtras): Pick<UiCitation, 'url' | 'webTitle'> {
  const out: Pick<UiCitation, 'url' | 'webTitle'> = {};
  if (typeof extras.url === 'string' && extras.url.trim()) out.url = extras.url;
  if (typeof extras.webTitle === 'string' && extras.webTitle.trim()) out.webTitle = extras.webTitle;
  return out;
}

export function fromRestCitation(c: Citation & WebCitationExtras): UiCitation {
  return {
    id: c.id,
    documentId: c.document_id,
    documentName: c.document_name,
    chunkId: c.chunk_id,
    snippet: c.snippet,
    charStart: c.char_start,
    charEnd: c.char_end,
    ...(c.score !== undefined ? { score: c.score } : {}),
    ...(c.redacted ? { redacted: true } : {}),
    ...webExtras(c),
  };
}

export function fromWsCitation(c: ChatCitation & WebCitationExtras): UiCitation {
  return {
    id: c.id,
    documentId: c.documentId,
    documentName: c.documentName,
    chunkId: c.chunkId,
    snippet: c.snippet,
    charStart: c.charStart,
    charEnd: c.charEnd,
    ...(c.score !== undefined ? { score: c.score } : {}),
    ...webExtras(c),
  };
}
