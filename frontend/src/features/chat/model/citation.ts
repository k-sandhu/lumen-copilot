/**
 * Normalize persisted REST and live WebSocket evidence into one UI model.
 * Corpus passages and public web evidence are separate variants: web citations
 * carry no document/chunk ids and are accepted only with a safe HTTP(S) URL.
 */
import type { ChatCitation, ChatWebCitation, Citation, WebCitation } from '@/api';
import { validMediaTimeSpan } from '@/lib/mediaTime';

export interface UiDocumentCitation {
  kind: 'document';
  id: string;
  handle?: string | null;
  documentId: string;
  documentName: string;
  chunkId: string;
  snippet: string;
  charStart: number;
  charEnd: number;
  timeStartMs?: number;
  timeEndMs?: number;
  transcriptSegmentId?: string;
  speakerId?: string;
  speakerName?: string;
  score?: number;
  /** Permission was revoked since the answer was produced. */
  redacted?: boolean;
}

export interface UiWebCitation {
  kind: 'web';
  id: string;
  handle: string;
  url: string;
  webTitle: string;
  snippet: string;
}

export type UiCitation = UiDocumentCitation | UiWebCitation;
export type CitationKind = UiCitation['kind'];

export function kindOfCitation(citation: UiCitation): CitationKind {
  return citation.kind;
}

/** The parsed host of a safe HTTP(S) URL, with www stripped. */
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

/** True only for an HTTP(S) URL; the gate for rendering an external link. */
export function isSafeHttpUrl(url: string | undefined): url is string {
  return hostOf(url) !== null;
}

function restMediaFields(
  citation: Citation,
): Pick<
  UiDocumentCitation,
  'timeStartMs' | 'timeEndMs' | 'transcriptSegmentId' | 'speakerId' | 'speakerName'
> {
  const span = validMediaTimeSpan(citation.time_start_ms, citation.time_end_ms);
  if (!span) return {};
  return {
    timeStartMs: span.startMs,
    timeEndMs: span.endMs,
    ...(typeof citation.transcript_segment_id === 'string'
      ? { transcriptSegmentId: citation.transcript_segment_id }
      : {}),
    ...(typeof citation.speaker_id === 'string' ? { speakerId: citation.speaker_id } : {}),
    ...(typeof citation.speaker_name === 'string' ? { speakerName: citation.speaker_name } : {}),
  };
}

function wsMediaFields(
  citation: ChatCitation,
): Pick<
  UiDocumentCitation,
  'timeStartMs' | 'timeEndMs' | 'transcriptSegmentId' | 'speakerId' | 'speakerName'
> {
  const span = validMediaTimeSpan(citation.timeStartMs, citation.timeEndMs);
  if (!span) return {};
  return {
    timeStartMs: span.startMs,
    timeEndMs: span.endMs,
    ...(typeof citation.transcriptSegmentId === 'string'
      ? { transcriptSegmentId: citation.transcriptSegmentId }
      : {}),
    ...(typeof citation.speakerId === 'string' ? { speakerId: citation.speakerId } : {}),
    ...(typeof citation.speakerName === 'string' ? { speakerName: citation.speakerName } : {}),
  };
}

export function fromRestCitation(citation: Citation): UiDocumentCitation {
  return {
    kind: 'document',
    id: citation.id,
    ...(citation.handle !== undefined ? { handle: citation.handle } : {}),
    documentId: citation.document_id,
    documentName: citation.document_name,
    chunkId: citation.chunk_id,
    snippet: citation.snippet,
    charStart: citation.char_start,
    charEnd: citation.char_end,
    ...restMediaFields(citation),
    ...(citation.score !== undefined ? { score: citation.score } : {}),
    ...(citation.redacted ? { redacted: true } : {}),
  };
}

export function fromWsCitation(citation: ChatCitation): UiDocumentCitation {
  return {
    kind: 'document',
    id: citation.id,
    ...(citation.handle !== undefined ? { handle: citation.handle } : {}),
    documentId: citation.documentId,
    documentName: citation.documentName,
    chunkId: citation.chunkId,
    snippet: citation.snippet,
    charStart: citation.charStart,
    charEnd: citation.charEnd,
    ...wsMediaFields(citation),
    ...(citation.score !== undefined ? { score: citation.score } : {}),
  };
}

/** Normalize separate public-web evidence; unsafe or malformed URLs are withheld. */
export function fromRestWebCitation(citation: WebCitation): UiWebCitation | null {
  if (!isSafeHttpUrl(citation.url) || !/^W[1-9][0-9]*$/.test(citation.handle)) return null;
  return {
    kind: 'web',
    id: citation.id,
    handle: citation.handle,
    url: citation.url,
    webTitle: citation.title,
    snippet: citation.snippet,
  };
}

/** Normalize a live public-web event with the same safe URL gate as REST. */
export function fromWsWebCitation(citation: ChatWebCitation): UiWebCitation | null {
  return fromRestWebCitation({
    id: citation.id,
    handle: citation.handle,
    url: citation.url,
    title: citation.title,
    snippet: citation.snippet,
  });
}
