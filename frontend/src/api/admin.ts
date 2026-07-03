/**
 * Typed admin-console calls — part of the api/ boundary (ADR-0004). The ONLY
 * place the SPA performs admin HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §admin, M2 #80 + #223). Restricted to the `admin` role;
 * any other role receives 403 (INV-5), and all are tenant-scoped (INV-1):
 *
 *   GET   /admin/members          ?cursor&limit → MemberList
 *   GET   /admin/model-governance               → ModelGovernance
 *   GET   /admin/risk-tiers                      → RiskTierList
 *   GET   /admin/tool-policy                      → ToolPolicy
 *   PATCH /admin/tool-policy      {tool_name,…}   → ToolPolicy
 *   PUT    /admin/branding        (multipart file) → TenantBranding
 *   DELETE /admin/branding                         → 204
 *
 * The tool-policy PATCH (issue #223) is the one governance WRITE here — a
 * tenant-scoped T1 action, audited server-side; an unknown tool name → 422 (INV-8).
 * Negative paths surface as typed `ApiError`s: 401 (INV-4) for a missing/expired
 * token, 403 (INV-5) for a non-admin caller.
 */
import { ApiError, request, withRefreshRetry } from './client';
import { API_BASE_URL } from './env';
import { getAccessToken } from './token';
import type {
  MemberList,
  ModelGovernance,
  Problem,
  RiskTierList,
  SandboxPolicy,
  SandboxPolicyUpdate,
  TenantBranding,
  ToolPolicy,
  ToolPolicyUpdate,
} from './types';

function joinApi(path: string): string {
  const b = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.title === 'string' && typeof v.status === 'number';
}

async function safeProblem(response: Response): Promise<Problem | undefined> {
  try {
    const data: unknown = await response.json();
    if (isProblem(data)) return data;
  } catch {
    // Non-JSON / empty body — fall through.
  }
  return undefined;
}

export interface PageQuery {
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/** The tenant's members and their roles (admin only). */
export function listMembers(page: PageQuery = {}, signal?: AbortSignal): Promise<MemberList> {
  return request<MemberList>(`/admin/members${buildQuery({ ...page })}`, { signal });
}

/** Which models are allowed, by governance tier (admin only). */
export function getModelGovernance(signal?: AbortSignal): Promise<ModelGovernance> {
  return request<ModelGovernance>('/admin/model-governance', { signal });
}

/** The read-before-write risk tiers T0–T3 (admin only). */
export function getRiskTiers(signal?: AbortSignal): Promise<RiskTierList> {
  return request<RiskTierList>('/admin/risk-tiers', { signal });
}

/** The per-tenant tool-governance policy — one entry per registered tool (admin only). */
export function getToolPolicy(signal?: AbortSignal): Promise<ToolPolicy> {
  return request<ToolPolicy>('/admin/tool-policy', { signal });
}

/**
 * Set the per-tenant `enabled` / `requires_approval` flags for one tool (admin only,
 * audited). Setting a gated tool's `requires_approval` to false (with `enabled`) is
 * the admin pre-approval that lets the approval gate execute it. An unknown tool
 * name → 422 (INV-8). Returns the full resulting policy.
 */
export function updateToolPolicy(body: ToolPolicyUpdate): Promise<ToolPolicy> {
  return request<ToolPolicy>('/admin/tool-policy', { method: 'PATCH', json: body });
}

/** The per-tenant code-execution sandbox policy (admin only, effective/clamped, #233). */
export function getSandboxPolicy(signal?: AbortSignal): Promise<SandboxPolicy> {
  return request<SandboxPolicy>('/admin/sandbox-policy', { signal });
}

/**
 * Set the per-tenant sandbox policy (admin only, audited, #233). The server clamps each
 * cap DOWN to the deploy-wide config ceiling and strips the cloud-metadata IP from the
 * egress allowlist (a per-tenant value can only narrow). A non-positive cap → 422
 * (INV-8). Returns the resulting effective policy.
 */
export function updateSandboxPolicy(body: SandboxPolicyUpdate): Promise<SandboxPolicy> {
  return request<SandboxPolicy>('/admin/sandbox-policy', { method: 'PATCH', json: body });
}

/**
 * Upload the tenant's application logo via `PUT /admin/branding` (multipart, admin
 * only, audited). Uses `fetch` + `FormData` (no JSON body): the browser sets the
 * multipart boundary; the bearer token is attached and the shared 401-refresh-retry
 * policy applies. A 413 (over-size) / 415 (non-image) / 403 (non-admin) surfaces as
 * a typed `ApiError` carrying the Problem body. Returns the new `{ logo_url }`.
 */
export function updateTenantBranding(file: File): Promise<TenantBranding> {
  return withRefreshRetry<TenantBranding>(async () => {
    const url = joinApi('/admin/branding');
    const headers = new Headers();
    headers.set('Accept', 'application/json, application/problem+json');
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    // NB: do NOT set Content-Type — the browser adds the multipart boundary.
    const form = new FormData();
    form.append('file', file);

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'PUT',
        credentials: 'include',
        headers,
        body: form,
      });
    } catch (cause) {
      throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
    }
    if (!response.ok) {
      throw new ApiError(
        `Request to ${url} failed with ${response.status}`,
        response.status,
        await safeProblem(response),
      );
    }
    return (await response.json()) as TenantBranding;
  });
}

/**
 * Clear the tenant's application logo via `DELETE /admin/branding` (admin only,
 * audited) so the shell reverts to the default brand mark. Idempotent (204).
 */
export function clearTenantBranding(): Promise<void> {
  return request<void>('/admin/branding', { method: 'DELETE' });
}
