/**
 * Public surface of the admin feature slice (issue #88). Routes and other modules
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature
 * deep imports). The whole slice is read-only governance (ADR-0007 §4).
 */
export { AdminPage } from './components/AdminPage';
export { AdminHeader } from './components/AdminHeader';
export { AdminTabs, type AdminTab } from './components/AdminTabs';
export { adminTabIds } from './components/tabIds';
export { MembersPanel } from './components/MembersPanel';
export { ModelGovernancePanel } from './components/ModelGovernancePanel';
export { RiskTierPanel } from './components/RiskTierPanel';
export { DataMinimizationPanel } from './components/DataMinimizationPanel';
export {
  useMembers,
  useModelGovernance,
  useRiskTiers,
  membersQueryKey,
  modelGovernanceQueryKey,
  riskTiersQueryKey,
} from './model/queries';
