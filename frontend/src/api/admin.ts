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
 *
 * The tool-policy PATCH (issue #223) is the one governance WRITE here — a
 * tenant-scoped T1 action, audited server-side; an unknown tool name → 422 (INV-8).
 * Negative paths surface as typed `ApiError`s: 401 (INV-4) for a missing/expired
 * token, 403 (INV-5) for a non-admin caller.
 */
import { request } from './client';
import type {
  LlmProvider,
  LlmProviderCreate,
  LlmProviderList,
  LlmProviderUpdate,
  MemberList,
  ModelGovernance,
  RiskTierList,
  SandboxPolicy,
  SandboxPolicyUpdate,
  ToolPolicy,
  ToolPolicyUpdate,
} from './types';

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

// --- LLM providers (per-tenant registration + model auto-discovery) ---
//
// Foundation PR: a tenant admin registers OpenAI-compatible providers; the backend
// auto-discovers each provider's models. The stored API key is WRITE-ONLY — sent in
// the create/update `api_key` field, never returned; the response carries only a
// masked `secret_hint`. All admin-only (403 otherwise, INV-5) + tenant-scoped
// (INV-1): a cross-tenant id → 404. Routing chat/embeddings through a provider is a
// SEPARATE follow-up PR.

/** The tenant's registered LLM providers, each with discovered models + status (admin only). */
export function listLlmProviders(signal?: AbortSignal): Promise<LlmProviderList> {
  return request<LlmProviderList>('/admin/llm-providers', { signal });
}

/**
 * Register an LLM provider and auto-discover its models (admin only). The server
 * validates the provider type + https/SSRF of `base_url` (unsupported/blocked → 422)
 * and stores any `api_key` write-only via CC-C. The returned provider is `ready` with
 * `discovered_models` on a reachable provider, or `error` + `last_error` on a bad
 * key/url (never a 500).
 */
export function createLlmProvider(body: LlmProviderCreate): Promise<LlmProvider> {
  return request<LlmProvider>('/admin/llm-providers', { method: 'POST', json: body });
}

/**
 * Update a registered LLM provider (rename, retarget `base_url`, toggle `enabled`, or
 * rotate/clear the write-only `api_key` — send `api_key: null` to clear). At least one
 * field. Changing `base_url` or rotating the key re-runs model discovery. A blocked
 * URL → 422; a cross-tenant id → 404.
 */
export function updateLlmProvider(id: string, body: LlmProviderUpdate): Promise<LlmProvider> {
  return request<LlmProvider>(`/admin/llm-providers/${id}`, { method: 'PATCH', json: body });
}

/** Remove a registered LLM provider and its stored API key (204). A cross-tenant id → 404. */
export function deleteLlmProvider(id: string): Promise<void> {
  return request<void>(`/admin/llm-providers/${id}`, { method: 'DELETE' });
}

/**
 * Re-run model discovery for a provider (admin only). Returns the UPDATED provider: a
 * reachable provider → `status: ready` (+ a fresh `discovered_models`); an
 * unreachable/erroring probe → `status: error` (+ a safe `last_error`). BOTH outcomes
 * return 200 — the result is in `status`, not the HTTP code. A cross-tenant id → 404.
 */
export function refreshLlmProvider(id: string): Promise<LlmProvider> {
  return request<LlmProvider>(`/admin/llm-providers/${id}/refresh`, { method: 'POST' });
}
