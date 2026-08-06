/**
 * Public surface of the admin feature slice (issue #88, #223, #540). Routes and
 * other modules import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports). The slice is mostly read-only governance
 * (ADR-0007 §4); the write surfaces are Tool governance (#223) — an admin
 * enables / disables tools per tenant and sets the approval policy the runner's
 * approval gate consults — and Groups (#540, ADR-0022), the tenant-scoped
 * membership a source grant can name.
 */
export { AdminPage } from './components/AdminPage';
export { AdminHeader } from './components/AdminHeader';
export { AdminTabs, type AdminTab } from './components/AdminTabs';
export { adminTabIds } from './components/tabIds';
export { MembersPanel } from './components/MembersPanel';
export { GroupsPanel } from './components/GroupsPanel';
export { GroupMembersSection } from './components/GroupMembersSection';
export { ModelGovernancePanel } from './components/ModelGovernancePanel';
export { RiskTierPanel } from './components/RiskTierPanel';
export { ToolGovernancePanel } from './components/ToolGovernancePanel';
export { SandboxGovernancePanel } from './components/SandboxGovernancePanel';
export { DataMinimizationPanel } from './components/DataMinimizationPanel';
export {
  useMembers,
  useAttestMemberIdentity,
  useGroups,
  useGroupMembers,
  useCreateGroup,
  useRenameGroup,
  useDeleteGroup,
  useAddGroupMember,
  useRemoveGroupMember,
  groupKeys,
  useModelGovernance,
  useRiskTiers,
  useToolPolicy,
  useUpdateToolPolicy,
  useSandboxPolicy,
  useUpdateSandboxPolicy,
  membersQueryKey,
  modelGovernanceQueryKey,
  riskTiersQueryKey,
  toolPolicyQueryKey,
  sandboxPolicyQueryKey,
} from './model/queries';
