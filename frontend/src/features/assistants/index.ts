/**
 * Public surface of the assistants feature slice (#212, ADR-0011). Routes and
 * other features import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { AssistantsPage } from './components/AssistantsPage';
export { AssistantLibrary } from './components/AssistantLibrary';
export { AssistantCard } from './components/AssistantCard';
export { AssistantEditor } from './components/AssistantEditor';
export { DescribeAssistant } from './components/DescribeAssistant';
export { VersionHistory } from './components/VersionHistory';
export {
  useAssistants,
  useAssistant,
  useCreateAssistant,
  useDraftAssistant,
  useUpdateAssistant,
  usePublishAssistant,
  useDeleteAssistant,
  useAssistantVersions,
  useRollbackAssistant,
  assistantKeys,
} from './model/queries';
export {
  emptyForm,
  formFromAssistant,
  formFromDraft,
  toCreateBody,
  toUpdateBody,
  canPublish,
  type AssistantFormState,
} from './model/form';
export { toolLabel, toToolOption, toToolOptions, type ToolOption } from './model/tools';
