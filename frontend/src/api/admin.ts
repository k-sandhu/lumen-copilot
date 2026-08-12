/**
 * Typed admin-console calls — part of the api/ boundary (ADR-0004). The ONLY
 * place the SPA performs admin HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §admin, M2 #80 + #223). Restricted to the `admin` role;
 * any other role receives 403 (INV-5), and all are tenant-scoped (INV-1):
 *
 *   GET    /admin/members         ?cursor&limit → MemberList
 *   GET    /admin/model-governance              → ModelGovernance
 *   GET    /admin/risk-tiers                     → RiskTierList
 *   GET    /admin/tool-policy                     → ToolPolicy
 *   PATCH  /admin/tool-policy     {tool_name,…}   → ToolPolicy
 *   GET    /admin/groups                          → GroupList
 *   POST   /admin/groups          {name}          → Group
 *   GET    /admin/groups/{id}                     → Group
 *   PATCH  /admin/groups/{id}     {name}          → Group
 *   DELETE /admin/groups/{id}                     → 204
 *   GET    /admin/groups/{id}/members             → GroupMemberList
 *   POST   /admin/groups/{id}/members {user_id}   → 204
 *   DELETE /admin/groups/{id}/members/{userId}    → 204
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
  LlmProvider,
  LlmProviderList,
  LlmProviderCreate,
  LlmProviderUpdate,
  Problem,
  TenantBranding,
  AutonomyPolicy,
  AutonomyPolicyUpdate,
  CertificationState,
  GovernedAssistant,
  GovernedAssistantList,
  Group,
  GroupCreate,
  GroupList,
  GroupMemberAdd,
  GroupMemberList,
  GroupUpdate,
  Member,
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

/**
 * Attest a member's email identity for connector-ACL mapping (ADR-0019 §2;
 * admin only, audited server-side as `user.identity_attested`). Mirrored
 * connector ACLs map a source permission to a Lumen user ONLY when that user's
 * email is attested — unattested members never receive mirrored access.
 * Idempotent: re-attesting refreshes `email_attested_at`. A non-admin → 403
 * (INV-5); a cross-tenant / unknown member → 404 (INV-1). Returns the member
 * with their new attestation state.
 */
export function attestMemberIdentity(memberId: string): Promise<Member> {
  return request<Member>(`/admin/members/${memberId}/attest-identity`, { method: 'POST' });
}

/**
 * Every group in this tenant — the derived "All members" group first, then user
 * groups by name (ADR-0022). Not paginated: a tenant has few groups.
 */
export function listGroups(signal?: AbortSignal): Promise<GroupList> {
  return request<GroupList>('/admin/groups', { signal });
}

/**
 * Create a user group (admin only, audited as `group.created`). Names are
 * trimmed and unique per tenant case-insensitively → 409 `group_name_taken`;
 * "All members" is reserved for the derived tenant-wide group → 409
 * `group_name_reserved`. A blank or over-long (>120) name → 422 (INV-8).
 */
export function createGroup(body: GroupCreate): Promise<Group> {
  return request<Group>('/admin/groups', { method: 'POST', json: body });
}

/** One group in this tenant; unknown or cross-tenant → 404, never 403 (INV-1). */
export function getGroup(groupId: string, signal?: AbortSignal): Promise<Group> {
  return request<Group>(`/admin/groups/${groupId}`, { signal });
}

/**
 * Rename a user group (admin only, audited as `group.updated`). The derived
 * system group is immutable → 409 `system_group_immutable`; a colliding name →
 * 409 `group_name_taken`; the reserved name → 409 `group_name_reserved`.
 */
export function renameGroup(groupId: string, body: GroupUpdate): Promise<Group> {
  return request<Group>(`/admin/groups/${groupId}`, { method: 'PATCH', json: body });
}

/**
 * Delete a user group (admin only, audited as `group.deleted`). Also drops its
 * memberships AND every grant naming it, so the access it conferred stops
 * immediately. The system group cannot be deleted → 409 `system_group_immutable`.
 */
export function deleteGroup(groupId: string): Promise<void> {
  return request<void>(`/admin/groups/${groupId}`, { method: 'DELETE' });
}

/**
 * The users explicitly in a group, ordered by email. Empty for the system
 * group, whose membership is derived rather than enumerated (ADR-0022 §3).
 */
export function listGroupMembers(groupId: string, signal?: AbortSignal): Promise<GroupMemberList> {
  return request<GroupMemberList>(`/admin/groups/${groupId}/members`, { signal });
}

/**
 * Add a user to a group (admin only, audited as `group.member_added`).
 * Idempotent — re-adding an existing member succeeds silently. The user must be
 * in the caller's tenant, else 404 (INV-1); the system group cannot be
 * populated → 409 `system_group_immutable`.
 */
export function addGroupMember(groupId: string, body: GroupMemberAdd): Promise<void> {
  return request<void>(`/admin/groups/${groupId}/members`, { method: 'POST', json: body });
}

/**
 * Remove a user from a group (admin only, audited as `group.member_removed`).
 * Idempotent. Membership is re-read per request and never cached in the token,
 * so the user loses group-granted access on their NEXT request (ADR-0022 §7).
 */
export function removeGroupMember(groupId: string, memberId: string): Promise<void> {
  return request<void>(`/admin/groups/${groupId}/members/${memberId}`, { method: 'DELETE' });
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

// --- Assistant library governance (E6-6/E6-8, #217) ------------------------

/**
 * Every assistant in the tenant with its governance state (admin only, #217). Spans
 * every owner in the tenant; each item carries its certification / featured / disabled
 * state and the `ownerOrphaned` flag (owner deprovisioned — flag for reassignment).
 */
export function listGovernedAssistants(
  page: PageQuery = {},
  signal?: AbortSignal,
): Promise<GovernedAssistantList> {
  return request<GovernedAssistantList>(`/admin/assistants${buildQuery({ ...page })}`, { signal });
}

/**
 * Set an assistant's certification verdict — certify / deprecate / clear (admin only,
 * audited). A cross-tenant / missing id → 404 (existence non-disclosure).
 */
export function certifyAssistant(
  assistantId: string,
  certificationState: CertificationState,
): Promise<GovernedAssistant> {
  return request<GovernedAssistant>(`/admin/assistants/${assistantId}/certify`, {
    method: 'POST',
    json: { certificationState },
  });
}

/** Feature / unfeature an assistant in the library (admin only, audited). */
export function featureAssistant(
  assistantId: string,
  featured: boolean,
): Promise<GovernedAssistant> {
  return request<GovernedAssistant>(`/admin/assistants/${assistantId}/feature`, {
    method: 'POST',
    json: { featured },
  });
}

/**
 * Disable / re-enable an assistant (admin only, audited). Disabling blocks it from
 * starting a chat / schedule / run; re-enabling returns the head to `draft`.
 */
export function disableAssistant(
  assistantId: string,
  disabled: boolean,
): Promise<GovernedAssistant> {
  return request<GovernedAssistant>(`/admin/assistants/${assistantId}/disable`, {
    method: 'POST',
    json: { disabled },
  });
}

/**
 * Reassign an assistant's accountable owner to another tenant member (admin only,
 * audited). The new owner must be a distinct member of the tenant (else 422). Rescues
 * an orphaned assistant.
 */
export function transferAssistantOwnership(
  assistantId: string,
  newOwner: string,
): Promise<GovernedAssistant> {
  return request<GovernedAssistant>(`/admin/assistants/${assistantId}/transfer-ownership`, {
    method: 'POST',
    json: { newOwner },
  });
}

/** The outcome of a bulk reassign/disable of orphaned assistants (E6-8, #217). */
export interface BulkOrphanResult {
  affected: string[];
  action: string;
}

/**
 * Disable every orphaned assistant (owner deprovisioned) in the tenant (admin only,
 * audited). Idempotent — an already-disabled orphan is skipped.
 */
export function disableOrphanedAssistants(): Promise<BulkOrphanResult> {
  return request<BulkOrphanResult>('/admin/assistants/disable-orphans', { method: 'POST' });
}

export function getAutonomyPolicy(signal?: AbortSignal): Promise<AutonomyPolicy> {
  return request<AutonomyPolicy>('/admin/autonomy-policy', { signal });
}

/**
 * Set the per-tenant assistant autonomy cap (admin only, audited, #218). The cap only
 * ever NARROWS — it lowers an assistant's effective autonomy, never raises it. An
 * unknown `max_autonomy` → 422 (INV-8). Returns the resulting cap.
 */
export function updateAutonomyPolicy(body: AutonomyPolicyUpdate): Promise<AutonomyPolicy> {
  return request<AutonomyPolicy>('/admin/autonomy-policy', { method: 'PATCH', json: body });
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
export function createLlmProvider(
  body: LlmProviderCreate,
  signal?: AbortSignal,
): Promise<LlmProvider> {
  return request<LlmProvider>('/admin/llm-providers', { method: 'POST', json: body, signal });
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
