/**
 * Public surface of the admin feature slice (issue #88, #223). Routes and other
 * modules import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports). The slice is mostly read-only governance (ADR-0007
 * §4); the one write surface is Tool governance (issue #223) — an admin enables /
 * disables tools per tenant and sets the approval policy the runner's approval gate
 * consults.
 */
export { AdminPage } from './components/AdminPage';
export { AdminHeader } from './components/AdminHeader';
export { AdminTabs, type AdminTab } from './components/AdminTabs';
export { adminTabIds } from './components/tabIds';
export { MembersPanel } from './components/MembersPanel';
export { ModelGovernancePanel } from './components/ModelGovernancePanel';
export { RiskTierPanel } from './components/RiskTierPanel';
export { ToolGovernancePanel } from './components/ToolGovernancePanel';
export { DataMinimizationPanel } from './components/DataMinimizationPanel';
export {
  useMembers,
  useModelGovernance,
  useRiskTiers,
  useToolPolicy,
  useUpdateToolPolicy,
  membersQueryKey,
  modelGovernanceQueryKey,
  riskTiersQueryKey,
  toolPolicyQueryKey,
} from './model/queries';
