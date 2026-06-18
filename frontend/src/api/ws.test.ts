import { describe, it, expect } from 'vitest';
import { parseEnvelope, resolveWsUrl } from './ws';

describe('parseEnvelope', () => {
  it('parses a valid start envelope', () => {
    const env = parseEnvelope(JSON.stringify({ type: 'start', streamId: 's1', seq: 0 }));
    expect(env.type).toBe('start');
    expect(env.streamId).toBe('s1');
    expect(env.seq).toBe(0);
  });

  it('parses an error envelope with a problem', () => {
    const env = parseEnvelope(
      JSON.stringify({
        type: 'error',
        streamId: 's1',
        seq: 4,
        problem: { title: 'Boom', status: 500 },
      }),
    );
    expect(env.type).toBe('error');
    if (env.type === 'error') {
      expect(env.problem.status).toBe(500);
    }
  });

  it('rejects an unknown type', () => {
    expect(() => parseEnvelope(JSON.stringify({ type: 'nope', streamId: 's', seq: 0 }))).toThrow();
  });

  it('rejects a missing streamId/seq', () => {
    expect(() => parseEnvelope(JSON.stringify({ type: 'delta' }))).toThrow();
  });
});

describe('resolveWsUrl', () => {
  it('passes through absolute ws urls', () => {
    expect(resolveWsUrl('ws://host:9/ws', '/health')).toBe('ws://host:9/ws/health');
  });

  it('derives scheme/host from location for same-origin paths', () => {
    // jsdom default location is http://localhost/.
    const url = resolveWsUrl('/ws', '/health');
    expect(url.startsWith('ws://')).toBe(true);
    expect(url.endsWith('/ws/health')).toBe(true);
  });

  it('appends the bearer access token as a query param when present (#48)', () => {
    // The WS handshake can't set Authorization headers, so the token rides as
    // ?access_token=… and the backend validates it in auth/ (INV-4).
    expect(resolveWsUrl('ws://host/ws', '/chat', 'jwt-abc')).toBe(
      'ws://host/ws/chat?access_token=jwt-abc',
    );
  });

  it('url-encodes the token and omits the param when tokenless', () => {
    expect(resolveWsUrl('ws://host/ws', '/chat', 'a/b+c')).toBe(
      'ws://host/ws/chat?access_token=a%2Fb%2Bc',
    );
    expect(resolveWsUrl('ws://host/ws', '/chat')).toBe('ws://host/ws/chat');
    expect(resolveWsUrl('ws://host/ws', '/chat', null)).toBe('ws://host/ws/chat');
  });
});
