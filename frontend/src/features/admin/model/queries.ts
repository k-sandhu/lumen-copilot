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
  clearTenantBranding,
  getModelGovernance,
  getRiskTiers,
  getSandboxPolicy,
  getToolPolicy,
  listMembers,
  updateSandboxPolicy,
  updateTenantBranding,
  updateToolPolicy,
} from '@/api';
import type {
  MemberList,
  ModelGovernance,
  RiskTierList,
  SandboxPolicy,
  SandboxPolicyUpdate,
  TenantBranding,
  ToolPolicy,
  ToolPolicyUpdate,
} from '@/api';
import { currentUserQueryKey } from '@/features/auth';

export const membersQueryKey = ['admin', 'members'] as const;
export const modelGovernanceQueryKey = ['admin', 'model-governance'] as const;
export const riskTiersQueryKey = ['admin', 'risk-tiers'] as const;
export const toolPolicyQueryKey = ['admin', 'tool-policy'] as const;
export const sandboxPolicyQueryKey = ['admin', 'sandbox-policy'] as const;

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

/**
 * Upload the tenant's application logo (admin branding). The current logo state is
 * carried on `GET /auth/me` (`logo_url`) — the source both the shell brand cell and
 * the branding panel read — so on success we invalidate that query, refreshing the
 * shell + panel in one step. A 413/415/403 propagates as an `ApiError` the panel
 * surfaces; it is NOT swallowed here.
 */
export function useUpdateTenantBranding(): UseMutationResult<TenantBranding, unknown, File> {
  const qc = useQueryClient();
  return useMutation<TenantBranding, unknown, File>({
    mutationFn: (file) => updateTenantBranding(file),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: currentUserQueryKey });
    },
  });
}

/**
 * Clear the tenant's application logo (admin branding) so the shell reverts to the
 * default brand mark. Invalidates `GET /auth/me` on success so the shell + panel
 * re-read. A 403 propagates as an `ApiError` the panel surfaces.
 */
export function useClearTenantBranding(): UseMutationResult<void, unknown, void> {
  const qc = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: () => clearTenantBranding(),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: currentUserQueryKey });
    },
  });
}
