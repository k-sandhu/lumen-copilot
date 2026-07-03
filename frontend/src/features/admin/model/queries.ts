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
  getModelGovernance,
  getRiskTiers,
  getToolPolicy,
  listMembers,
  updateToolPolicy,
} from '@/api';
import type {
  MemberList,
  ModelGovernance,
  RiskTierList,
  ToolPolicy,
  ToolPolicyUpdate,
} from '@/api';

export const membersQueryKey = ['admin', 'members'] as const;
export const modelGovernanceQueryKey = ['admin', 'model-governance'] as const;
export const riskTiersQueryKey = ['admin', 'risk-tiers'] as const;
export const toolPolicyQueryKey = ['admin', 'tool-policy'] as const;

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
