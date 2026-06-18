/**
 * Chat api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §chat) and that the
 * spec-0004 negative categories surface as typed ApiErrors the UI branches on:
 *   - cross-tenant / not-permitted resource → 404 (INV-1/INV-2)
 *   - malformed body → 422 (INV-8)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  createChatSession,
  deleteChatSession,
  getChatSession,
  listChatSessions,
  listMessages,
  sendMessage,
  updateChatSession,
} from '@/api';
import { setAccessToken, clearAccessToken } from '@/api';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

interface FetchSpy {
  mock: { calls: unknown[][] };
}

function lastCall(spy: FetchSpy) {
  const calls = spy.mock.calls;
  const call = calls[calls.length - 1];
  return { url: String(call?.[0]), init: call?.[1] as RequestInit };
}

describe('chat api boundary', () => {
  it('GET /chat/sessions bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listChatSessions();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/chat/sessions');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('POST /chat/sessions sends the create body', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ id: 's' }, 201));
    await createChatSession({ title: 'New', model: 'm1' });
    const { init } = lastCall(spy);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ title: 'New', model: 'm1' });
  });

  it('POST .../messages returns the user message + stream_id (AC-1 shape)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(
        {
          message: { id: 'm', session_id: 's', role: 'user', content: 'hi', citations: [], created_at: 'x' },
          stream_id: 'stream-1',
        },
        202,
      ),
    );
    const res = await sendMessage('s', { content: 'hi', model: 'm1', collection_ids: ['c1'] });
    expect(res.stream_id).toBe('stream-1');
    expect(res.message.role).toBe('user');
  });

  it('sends model + collection_ids on a message (AC-3 scoping)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ message: {}, stream_id: 's' }, 202));
    await sendMessage('s', { content: 'q', model: 'fast/x', collection_ids: ['c1', 'c2'] });
    const { init } = lastCall(spy);
    expect(JSON.parse(init.body as string)).toMatchObject({
      content: 'q',
      model: 'fast/x',
      collection_ids: ['c1', 'c2'],
    });
  });

  it('GET .../messages supports pagination params', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listMessages('s', { cursor: 'abc', limit: 50 });
    expect(lastCall(spy).url).toContain('cursor=abc');
    expect(lastCall(spy).url).toContain('limit=50');
  });

  it('PATCH renames / re-models a session', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ id: 's' }));
    await updateChatSession('s', { title: 'Renamed' });
    const { init } = lastCall(spy);
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ title: 'Renamed' });
  });

  it('DELETE issues a DELETE (204 → void)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(deleteChatSession('s')).resolves.toBeUndefined();
    expect(lastCall(spy).init.method).toBe('DELETE');
  });

  it('cross-tenant / not-permitted session → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(getChatSession('other-tenant-session')).rejects.toMatchObject({
      status: 404,
    });
    // It is an ApiError carrying the problem the UI branches on (not a raw throw).
    await expect(getChatSession('x')).rejects.toBeInstanceOf(ApiError);
  });

  it('malformed message body → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable Entity'));
    await expect(sendMessage('s', { content: '' })).rejects.toMatchObject({ status: 422 });
  });
});
