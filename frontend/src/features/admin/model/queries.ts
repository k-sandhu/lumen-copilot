/**
 * Server-state hooks for the admin slice — TanStack Query (#88, #223). The
 * governance READ surfaces are read-only server data (ADR-0007 §4): the member
 * roster, model governance, the risk-tier reference, and the per-tenant tool
 * policy. The one governance WRITE is the tool-policy PATCH (issue #223) — a
 * tenant-scoped, audited T1 action that flips a tool's per-tenant enabled /
 * requires-approval flags (the switch the approval gate consults; the run_python
 * unlock). Its mutation invalidates the policy query so the panel re-reads.
 *
 * Conforms to the frozen contract (contracts/openapi.yaml §admin, #80 + #223) via
 * the api/ boundary only — no transport in this slice (frontend/AGENTS.md).
 *
 * All are tenant-scoped (INV-1) and admin-only: a non-admin caller's 403 (INV-5)
 * and a missing/expired token's 401 (INV-4) surface as the query/mutation's
 * `error` (a typed `ApiError`), which each panel renders as an actionable state.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  createLlmProvider,
  deleteLlmProvider,
  getModelGovernance,
  getRiskTiers,
  getSandboxPolicy,
  getToolPolicy,
  listLlmProviders,
  listMembers,
  refreshLlmProvider,
  updateLlmProvider,
  updateSandboxPolicy,
  updateToolPolicy,
} from '@/api';
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
} from '@/api';

export const membersQueryKey = ['admin', 'members'] as const;
export const modelGovernanceQueryKey = ['admin', 'model-governance'] as const;
export const riskTiersQueryKey = ['admin', 'risk-tiers'] as const;
export const toolPolicyQueryKey = ['admin', 'tool-policy'] as const;
export const sandboxPolicyQueryKey = ['admin', 'sandbox-policy'] as const;
export const llmProvidersQueryKey = ['admin', 'llm-providers'] as const;

/** The tenant's members and their roles (admin only). */
export function useMembers(): UseQueryResult<MemberList> {
  return useQuery<MemberList>({
    queryKey: membersQueryKey,
    queryFn: ({ signal }) => listMembers({}, signal),
    staleTime: 30_000,
  });
}

/** Which models are allowed, by governance tier (admin only). */
export function useModelGovernance(): UseQueryResult<ModelGovernance> {
  return useQuery<ModelGovernance>({
    queryKey: modelGovernanceQueryKey,
    queryFn: ({ signal }) => getModelGovernance(signal),
    staleTime: 30_000,
  });
}

/** The read-before-write risk tiers T0–T3 (admin only). */
export function useRiskTiers(): UseQueryResult<RiskTierList> {
  return useQuery<RiskTierList>({
    queryKey: riskTiersQueryKey,
    queryFn: ({ signal }) => getRiskTiers(signal),
    // The tier reference is effectively static config; cache it generously.
    staleTime: 5 * 60_000,
  });
}

/** The per-tenant tool-governance policy — one entry per registered tool (admin only). */
export function useToolPolicy(): UseQueryResult<ToolPolicy> {
  return useQuery<ToolPolicy>({
    queryKey: toolPolicyQueryKey,
    queryFn: ({ signal }) => getToolPolicy(signal),
    staleTime: 15_000,
  });
}

/**
 * Set a tool's per-tenant flags (issue #223). On success we invalidate the policy
 * query so the panel re-reads the authoritative server state (the PATCH returns the
 * full policy, so we also seed the cache to avoid a flash). A 422 (unknown tool) /
 * 403 propagates as an `ApiError` the panel surfaces — it is NOT swallowed here.
 */
export function useUpdateToolPolicy(): UseMutationResult<ToolPolicy, unknown, ToolPolicyUpdate> {
  const qc = useQueryClient();
  return useMutation<ToolPolicy, unknown, ToolPolicyUpdate>({
    mutationFn: (body) => updateToolPolicy(body),
    onSuccess: (policy) => {
      qc.setQueryData(toolPolicyQueryKey, policy);
      void qc.invalidateQueries({ queryKey: toolPolicyQueryKey });
    },
  });
}

/** The per-tenant code-execution sandbox policy — effective/clamped (admin only, #233). */
export function useSandboxPolicy(): UseQueryResult<SandboxPolicy> {
  return useQuery<SandboxPolicy>({
    queryKey: sandboxPolicyQueryKey,
    queryFn: ({ signal }) => getSandboxPolicy(signal),
    staleTime: 15_000,
  });
}

/**
 * Set the per-tenant sandbox policy (issue #233). On success we seed the cache with the
 * returned effective (clamped) policy and invalidate so the panel re-reads. A 422
 * (non-positive cap) / 403 propagates as an `ApiError` the panel surfaces — it is NOT
 * swallowed here.
 */
export function useUpdateSandboxPolicy(): UseMutationResult<
  SandboxPolicy,
  unknown,
  SandboxPolicyUpdate
> {
  const qc = useQueryClient();
  return useMutation<SandboxPolicy, unknown, SandboxPolicyUpdate>({
    mutationFn: (body) => updateSandboxPolicy(body),
    onSuccess: (policy) => {
      qc.setQueryData(sandboxPolicyQueryKey, policy);
      void qc.invalidateQueries({ queryKey: sandboxPolicyQueryKey });
    },
  });
}

// --- LLM providers (per-tenant registration + model auto-discovery) ---
//
// Foundation PR: a tenant admin registers OpenAI-compatible providers and the backend
// auto-discovers each provider's models. Every mutation invalidates the providers
// query so the panel re-reads the authoritative server state (create/refresh return
// the discovered snapshot; delete has no body). A 422 (bad type/url) / 403 propagates
// as an `ApiError` the panel surfaces — it is NOT swallowed here.

/** The tenant's registered LLM providers, each with discovered models + status (admin only). */
export function useLlmProviders(): UseQueryResult<LlmProviderList> {
  return useQuery<LlmProviderList>({
    queryKey: llmProvidersQueryKey,
    queryFn: ({ signal }) => listLlmProviders(signal),
    staleTime: 15_000,
  });
}

/** Register a provider and auto-discover its models; invalidate the list on success. */
export function useCreateLlmProvider(): UseMutationResult<LlmProvider, unknown, LlmProviderCreate> {
  const qc = useQueryClient();
  return useMutation<LlmProvider, unknown, LlmProviderCreate>({
    mutationFn: (body) => createLlmProvider(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: llmProvidersQueryKey });
    },
  });
}

/** Update a provider (rename/retarget/toggle/rotate-key); invalidate the list on success. */
export function useUpdateLlmProvider(): UseMutationResult<
  LlmProvider,
  unknown,
  { id: string; body: LlmProviderUpdate }
> {
  const qc = useQueryClient();
  return useMutation<LlmProvider, unknown, { id: string; body: LlmProviderUpdate }>({
    mutationFn: ({ id, body }) => updateLlmProvider(id, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: llmProvidersQueryKey });
    },
  });
}

/** Delete a provider + its stored key; invalidate the list on success. */
export function useDeleteLlmProvider(): UseMutationResult<void, unknown, string> {
  const qc = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id) => deleteLlmProvider(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: llmProvidersQueryKey });
    },
  });
}

/** Re-run model discovery for a provider; invalidate the list on success. */
export function useRefreshLlmProvider(): UseMutationResult<LlmProvider, unknown, string> {
  const qc = useQueryClient();
  return useMutation<LlmProvider, unknown, string>({
    mutationFn: (id) => refreshLlmProvider(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: llmProvidersQueryKey });
    },
  });
}
