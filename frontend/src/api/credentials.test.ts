import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createLlmProvider, listLlmProviders } from './admin';
import { listMcpServers, registerMcpServer } from './mcpServers';
import { clearAccessToken, setAccessToken } from './token';
import { storageSnapshot } from '@/test/storageSnapshot';

beforeEach(() => setAccessToken('account-a-bearer'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('credential network and storage boundaries (#580)', () => {
  it.each(['provider', 'mcp'] as const)(
    'sends a new %s secret only in the explicit write body',
    async (kind) => {
      const secret = 'typed-secret-network-negative-580';
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
        new Response(JSON.stringify({ secret_hint: '****safe' }), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
      if (kind === 'provider') {
        await createLlmProvider({
          name: 'Test provider',
          provider_type: 'openai_compatible',
          base_url: 'https://provider.example/v1',
          api_key: secret,
        });
      } else {
        await registerMcpServer({
          name: 'Test server',
          transport: 'streamable_http',
          endpoint_url: 'https://mcp.example',
          auth: { type: 'bearer', value: secret },
        });
      }
      expect(fetchSpy).toHaveBeenCalledOnce();
      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(init?.method).toBe('POST');
      expect(String(init?.body)).toContain(secret);
      expect(String(url)).not.toContain(secret);
      expect(JSON.stringify([...new Headers(init?.headers)])).not.toContain(secret);
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer account-a-bearer');
      expect(storageSnapshot(localStorage)).not.toContain(secret);
      expect(storageSnapshot(sessionStorage)).not.toContain(secret);
      expect(window.location.href).not.toContain(secret);
    },
  );

  it('provider/MCP reads send no secret body and retain only the masked read shape', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ items: [{ id: 'presence-only', secret_hint: '****safe' }] }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
    );
    const responses = [await listLlmProviders(), await listMcpServers()];
    for (const response of responses) {
      expect(response.items[0]?.secret_hint).toBe('****safe');
      expect(response.items[0]).not.toHaveProperty('api_key');
      expect(response.items[0]).not.toHaveProperty('auth');
      expect(response.items[0]).not.toHaveProperty('value');
    }
    for (const [, init] of fetchSpy.mock.calls) expect(init?.body).toBeUndefined();
  });
});
