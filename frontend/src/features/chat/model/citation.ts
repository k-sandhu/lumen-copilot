/**
 * Citations arrive in two shapes that mean the same thing:
 *   - REST `Citation` (snake_case) — persisted, from GET .../messages.
 *   - WS `ChatCitation` (camelCase) — live, from event:citation.
 * Both mirror spec 0004 INV-3. This normalizes them to one UI shape so the
 * renderer and the viewer treat live and reloaded citations identically.
 */
import type { ChatCitation, Citation } from '@/api';

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
}

export function fromRestCitation(c: Citation): UiCitation {
  return {
    id: c.id,
    documentId: c.document_id,
    documentName: c.document_name,
    chunkId: c.chunk_id,
    snippet: c.snippet,
    charStart: c.char_start,
    charEnd: c.char_end,
    ...(c.score !== undefined ? { score: c.score } : {}),
  };
}

export function fromWsCitation(c: ChatCitation): UiCitation {
  return {
    id: c.id,
    documentId: c.documentId,
    documentName: c.documentName,
    chunkId: c.chunkId,
    snippet: c.snippet,
    charStart: c.charStart,
    charEnd: c.charEnd,
    ...(c.score !== undefined ? { score: c.score } : {}),
  };
}
