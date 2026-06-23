/**
 * Public surface of the audit feature slice (#86). Routes and other features
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature
 * deep imports).
 */
export { AuditPage } from './components/AuditPage';
export { AuditPanel } from './components/AuditPanel';
export { useAuditEvents, type AuditFilters } from './model/queries';
export {
  toKitRow,
  toProvenanceDetail,
  kindForEventType,
  eventTypeLabel,
} from './model/presentation';
