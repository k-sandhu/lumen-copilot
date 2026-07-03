/**
 * Public surface of the artifacts slice (#222). The panel (list / preview /
 * download / delete of agent-produced files) is the entry point; the preview body,
 * hooks, and pure helpers are exported for tests and cross-feature composition
 * (e.g. the code-run inspector resolving artifact chips to real links via
 * `artifactHref`).
 */
export { ArtifactPanel, type ArtifactPanelProps } from './components/ArtifactPanel';
export { ArtifactPreview, type ArtifactPreviewProps } from './components/ArtifactPreview';
export {
  useArtifacts,
  useDeleteArtifact,
  artifactKeys,
  type ArtifactFilters,
} from './model/queries';
export {
  formatBytes,
  fileKind,
  producedByLabel,
  previewKind,
  isMarkdown,
  relativeTime,
  type PreviewKind,
} from './model/presentation';
export { artifactHref, ARTIFACTS_ROUTE, ARTIFACT_PARAM } from './model/href';
