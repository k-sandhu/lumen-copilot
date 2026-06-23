/**
 * Public surface of the admin feature slice (issue #88). Routes and other modules
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature
 * deep imports). The whole slice is read-only governance (ADR-0007 §4).
 */
export { AdminPage } from './components/AdminPage';
export { MembersPanel } from './components/MembersPanel';
export { ModelGovernancePanel } from './components/ModelGovernancePanel';
export { RiskTierPanel } from './components/RiskTierPanel';
export {
  useMembers,
  useModelGovernance,
  useRiskTiers,
  membersQueryKey,
  modelGovernanceQueryKey,
  riskTiersQueryKey,
} from './model/queries';
