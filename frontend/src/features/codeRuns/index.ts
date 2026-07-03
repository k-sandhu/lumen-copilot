/**
 * Public surface of the code-run inspector slice (#232, E15-7 / E6-5). The
 * inline chat panel (CodeRunPanel) and the standalone detail view (CodeRunDetail)
 * are the entry points; the presentational inspector + model helpers are exported
 * for tests and cross-feature composition.
 */
export { CodeRunPanel, type CodeRunPanelProps } from './components/CodeRunPanel';
export { CodeRunDetail, type CodeRunDetailProps } from './components/CodeRunDetail';
export { CodeRunInspector, type CodeRunInspectorProps } from './components/CodeRunInspector';
export { useCodeRun, codeRunKeys } from './model/queries';
export {
  viewFromActivity,
  viewFromRecord,
  mergeView,
  type CodeRunView,
} from './model/view';
export {
  CODE_RUN_STATUS_LABEL,
  CODE_RUN_STATUS_TONE,
  CODE_RUN_STATUS_HEADLINE,
  isTerminalCodeRun,
  isFailureCodeRun,
  formatDuration,
  formatBytes,
  formatExitCode,
  usageRows,
  type UsageRow,
} from './model/presentation';
