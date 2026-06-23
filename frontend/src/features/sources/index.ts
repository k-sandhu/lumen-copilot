/**
 * Public surface of the sources feature slice (#27, ADR-0009). Routes and other
 * features import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { SourcesPage } from './components/SourcesPage';
export { SourcesPanel } from './components/SourcesPanel';
export { SourceCard } from './components/SourceCard';
export { AddSourceModal } from './components/AddSourceModal';
export { useSources, useCreateSource, useSyncSource, useDeleteSource } from './model/queries';
