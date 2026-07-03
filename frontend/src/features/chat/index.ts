/**
 * Public surface of the chat feature slice (issue #50). Routes/other features
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature
 * deep imports).
 */
export { ChatView } from './components/ChatView';
export { ModelPicker, type ModelPickerProps } from './components/ModelPicker';
export { KnowledgeModeChips, type KnowledgeMode } from './components/KnowledgeModeChips';
export {
  KnowledgeModeControl,
  type KnowledgeModeControlProps,
  type ModeAvailability,
} from './components/KnowledgeModeControl';
export { useChatStore } from './model/chatStore';
export { useChatStream } from './model/useChatStream';
export {
  buildRetrievalSummary,
  passageFromCitation,
  relativeTime,
  isStale,
  modelBadgeLabel,
  sourceMetadataRows,
  initialModes,
  modeAvailability,
  usedWebSearch,
  partitionCitations,
  DEFAULT_CHAT_MODES,
  WEB_DISABLED_REASON,
  type MetadataRow,
} from './model/presentation';
export {
  kindOfCitation,
  hostOf,
  isSafeHttpUrl,
  type UiCitation,
  type CitationKind,
} from './model/citation';
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
