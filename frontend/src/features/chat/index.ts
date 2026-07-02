/**
 * Public surface of the chat feature slice (issue #50). Routes/other features
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature
 * deep imports).
 */
export { ChatView } from './components/ChatView';
export { ModelPicker, type ModelPickerProps } from './components/ModelPicker';
export { KnowledgeModeChips, type KnowledgeMode } from './components/KnowledgeModeChips';
export { useChatStore } from './model/chatStore';
export { useChatStream } from './model/useChatStream';
export {
  buildRetrievalSummary,
  passageFromCitation,
  relativeTime,
  isStale,
  modelBadgeLabel,
  sourceMetadataRows,
  type MetadataRow,
} from './model/presentation';
export {
  useChatSessions,
  useMessages,
  useModels,
  useCreateSession,
  useUpdateSession,
  useDeleteSession,
  useSendMessage,
  chatKeys,
} from './model/queries';
