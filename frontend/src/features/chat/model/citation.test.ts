/**
 * Citation normalization + web-vs-document classification (#221). The wire does
 * not YET type a web citation distinctly, so the UI distinguishes by (safe) URL
 * presence — these tests pin that heuristic and the URL-safety gate that keeps a
 * web citation from ever producing an unsafe outbound link.
 */
import { describe, it, expect } from 'vitest';
import type { ChatCitation, Citation } from '@/api';
import {
  fromRestCitation,
  fromWsCitation,
  hostOf,
  isSafeHttpUrl,
  kindOfCitation,
  type WebCitationExtras,
} from './citation';

describe('kindOfCitation', () => {
  it('classifies a citation with a safe http(s) url as web', () => {
    expect(kindOfCitation({ url: 'https://example.com/a' })).toBe('web');
    expect(kindOfCitation({ url: 'http://example.com' })).toBe('web');
  });

  it('classifies a citation with no url as a document', () => {
    expect(kindOfCitation({})).toBe('document');
    expect(kindOfCitation({ url: '' })).toBe('document');
    expect(kindOfCitation({ url: '   ' })).toBe('document');
  });

  it('never promotes an unsafe (non-http) url to a web citation', () => {
    expect(kindOfCitation({ url: 'javascript:alert(1)' })).toBe('document');
    expect(kindOfCitation({ url: 'data:text/html,<x>' })).toBe('document');
    expect(kindOfCitation({ url: 'not a url' })).toBe('document');
  });
});

describe('hostOf', () => {
  it('returns the registrable host with www stripped', () => {
    expect(hostOf('https://www.en.wikipedia.org/wiki/X?y=1')).toBe('en.wikipedia.org');
    expect(hostOf('http://example.com:8080/path')).toBe('example.com');
  });

  it('returns null for an unparseable or non-http url', () => {
    expect(hostOf(undefined)).toBeNull();
    expect(hostOf('javascript:alert(1)')).toBeNull();
    expect(hostOf('nonsense')).toBeNull();
  });
});

describe('isSafeHttpUrl', () => {
  it('accepts only http(s) urls', () => {
    expect(isSafeHttpUrl('https://a.com')).toBe(true);
    expect(isSafeHttpUrl('http://a.com')).toBe(true);
    expect(isSafeHttpUrl('ftp://a.com')).toBe(false);
    expect(isSafeHttpUrl('javascript:void(0)')).toBe(false);
    expect(isSafeHttpUrl(undefined)).toBe(false);
  });
});

describe('normalization carries additive web fields', () => {
  it('fromRestCitation keeps url + webTitle when present', () => {
    const rest: Citation & WebCitationExtras = {
      id: 'c1',
      document_id: '',
      document_name: 'A page',
      chunk_id: '',
      snippet: 'text',
      char_start: 0,
      char_end: 4,
      url: 'https://example.com/x',
      webTitle: 'A page',
    };
    const ui = fromRestCitation(rest);
    expect(ui.url).toBe('https://example.com/x');
    expect(ui.webTitle).toBe('A page');
    expect(kindOfCitation(ui)).toBe('web');
  });

  it('fromWsCitation keeps url when present, omits it for a plain document', () => {
    const web: ChatCitation & WebCitationExtras = {
      id: 'w1',
      documentId: '',
      documentName: 'A page',
      chunkId: '',
      snippet: 'text',
      charStart: 0,
      charEnd: 4,
      url: 'https://example.com/x',
    };
    expect(fromWsCitation(web).url).toBe('https://example.com/x');

    const doc: ChatCitation & WebCitationExtras = {
      id: 'd1',
      documentId: 'doc-1',
      documentName: 'Doc.pdf',
      chunkId: 'k',
      snippet: 'text',
      charStart: 0,
      charEnd: 4,
    };
    const ui = fromWsCitation(doc);
    expect(ui.url).toBeUndefined();
    expect(kindOfCitation(ui)).toBe('document');
  });
});

describe('media citation normalization', () => {
  it('preserves timestamp, transcript segment and contextual speaker fields from REST and WS', () => {
    const rest: Citation = {
      id: 'media-rest',
      document_id: 'doc-media',
      document_name: 'meeting.mp4',
      chunk_id: 'chunk-1',
      snippet: 'Hello, my name is John.',
      char_start: 0,
      char_end: 23,
      time_start_ms: 12_500,
      time_end_ms: 18_000,
      transcript_segment_id: 'seg-1',
      speaker_id: 'speaker-1',
      speaker_name: 'John',
    };
    const ws: ChatCitation = {
      id: 'media-ws',
      documentId: 'doc-media',
      documentName: 'meeting.mp4',
      chunkId: 'chunk-1',
      snippet: rest.snippet,
      charStart: 0,
      charEnd: 23,
      timeStartMs: 12_500,
      timeEndMs: 18_000,
      transcriptSegmentId: 'seg-1',
      speakerId: 'speaker-1',
      speakerName: 'John',
    };

    expect(fromRestCitation(rest)).toMatchObject({
      timeStartMs: 12_500,
      timeEndMs: 18_000,
      transcriptSegmentId: 'seg-1',
      speakerId: 'speaker-1',
      speakerName: 'John',
    });
    expect(fromWsCitation(ws)).toMatchObject({
      timeStartMs: 12_500,
      timeEndMs: 18_000,
      transcriptSegmentId: 'seg-1',
      speakerId: 'speaker-1',
      speakerName: 'John',
    });
  });

  it('omits REST media fields serialized as null instead of inventing a zero seek', () => {
    const rest: Citation = {
      id: 'text-rest',
      document_id: 'doc-text',
      document_name: 'policy.pdf',
      chunk_id: 'chunk-text',
      snippet: 'Ordinary document passage.',
      char_start: 0,
      char_end: 26,
    };

    const ui = fromRestCitation(rest);
    expect(ui.timeStartMs).toBeUndefined();
    expect(ui.timeEndMs).toBeUndefined();
    expect(ui.transcriptSegmentId).toBeUndefined();
    expect(ui.speakerId).toBeUndefined();
    expect(ui.speakerName).toBeUndefined();
  });
});
