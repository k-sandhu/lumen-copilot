/**
 * Unit tests for the MCP-servers presentation helpers (#228, ADR-0012) — the pure
 * wire → trust-signal mappings, endpoint validation (the register form's UX
 * guard), and the ApiError → inline-message mapping (the 422 endpoint-blocked /
 * unsupported-transport reasons in particular). Side-effect-free, no transport.
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '@/api';
import type { McpServer } from './types';
import {
  endpointHost,
  lastCheckedLabel,
  registerErrorMessage,
  statusBadge,
  statusLabel,
  statusTone,
  transportLabel,
  validateEndpoint,
} from './presentation';

function makeServer(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: 'm1',
    name: 'Acme Ticketing',
    transport: 'streamable_http',
    endpoint_url: 'https://mcp.acme.com/sse',
    enabled: true,
    status: 'ready',
    last_health_at: '2026-07-03T11:50:00Z',
    discovered_tool_count: 4,
    secret_hint: '••••abcd',
    owner_id: 'u1',
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-03T11:50:00Z',
    ...overrides,
  };
}

describe('status mapping', () => {
  it('maps status → StatusDot tone', () => {
    expect(statusTone('ready')).toBe('ok');
    expect(statusTone('error')).toBe('danger');
    expect(statusTone('pending')).toBe('muted');
  });

  it('gives each status a human label + badge', () => {
    expect(statusLabel('ready')).toMatch(/healthy/i);
    expect(statusLabel('error')).toMatch(/unreachable/i);
    expect(statusBadge('error').modifier).toContain('danger');
    expect(statusBadge('pending').label).toMatch(/pending/i);
  });
});

describe('transport + endpoint helpers', () => {
  it('labels the transports', () => {
    expect(transportLabel('streamable_http')).toMatch(/streamable/i);
    expect(transportLabel('sse')).toBe('SSE');
  });

  it('extracts the endpoint host, falling back to the raw URL', () => {
    expect(endpointHost(makeServer())).toBe('mcp.acme.com');
    expect(endpointHost(makeServer({ endpoint_url: 'not a url' }))).toBe('not a url');
  });

  it('describes the last check, and says "never" before the first probe', () => {
    expect(lastCheckedLabel(makeServer({ status: 'pending', last_health_at: null }))).toMatch(
      /never/i,
    );
    const now = Date.parse('2026-07-03T12:00:00Z');
    expect(lastCheckedLabel(makeServer(), now)).toMatch(/checked/i);
  });
});

describe('validateEndpoint (register-form UX guard)', () => {
  it('accepts an absolute https URL', () => {
    const r = validateEndpoint('  https://mcp.example.com/sse  ');
    expect(r.ok).toBe(true);
    expect(r.url).toBe('https://mcp.example.com/sse');
  });

  it('rejects an empty value', () => {
    expect(validateEndpoint('   ').ok).toBe(false);
  });

  it('rejects a non-parseable URL', () => {
    expect(validateEndpoint('mcp.example.com').ok).toBe(false);
  });

  it('rejects plain http (https required)', () => {
    const r = validateEndpoint('http://mcp.example.com');
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/https/i);
  });
});

describe('registerErrorMessage', () => {
  function problem(status: number, code?: string): ApiError {
    return new ApiError('x', status, { type: 'about:blank', title: 't', status, code });
  }

  it('surfaces the SSRF endpoint-blocked reason (422 endpoint_blocked)', () => {
    expect(registerErrorMessage(problem(422, 'endpoint_blocked'))).toMatch(
      /blocked|private|reached safely/i,
    );
  });

  it('surfaces the unsupported-transport reason (422 unsupported_transport)', () => {
    expect(registerErrorMessage(problem(422, 'unsupported_transport'))).toMatch(
      /transport|streamable|sse/i,
    );
  });

  it('falls back to a generic 422 message', () => {
    expect(registerErrorMessage(problem(422))).toMatch(/couldn.t be registered|check the endpoint/i);
  });

  it('messages a 401 as a re-auth prompt', () => {
    expect(registerErrorMessage(problem(401))).toMatch(/session expired/i);
  });
});
