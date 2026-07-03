/**
 * Public surface of the api/ boundary. Import from here, not from deep paths.
 * This is the ONLY module the rest of the app uses to reach the backend.
 */
export { ApiError, request, registerRefreshHandler } from './client';
export type { RequestOptions } from './client';
export { getHealth, getReadiness } from './health';
export { login, refresh, getCurrentUser, logout, installAuthRefresh } from './auth';
export {
  listCollections,
  createCollection,
  updateCollection,
  deleteCollection,
  listDocuments,
  getDocument,
  deleteDocument,
  uploadDocument,
  fetchDocumentContent,
  fetchDocumentText,
} from './documents';
export type { PageQuery, UploadDocumentArgs, DocumentContent } from './documents';
export {
  listChatSessions,
  createChatSession,
  getChatSession,
  updateChatSession,
  deleteChatSession,
  listMessages,
  sendMessage,
} from './chat';
export type { PageParams } from './chat';
export { listModels } from './models';
export { search } from './search';
export { getPreferences, updatePreferences } from './preferences';
export {
  listSavedSearches,
  createSavedSearch,
  getSavedSearch,
  updateSavedSearch,
  deleteSavedSearch,
} from './savedSearches';
export { suggestSearch, listRecentSearches, clearRecentSearches } from './suggest';
export { listAuditEvents } from './audit';
export {
  listMembers,
  getModelGovernance,
  getRiskTiers,
  getToolPolicy,
  updateToolPolicy,
} from './admin';
// Note: sources.ts also exports a `PageQuery` identical to documents.ts's; the
// public one is already re-exported above, so we expose only the functions here.
export { listSources, createSource, syncSource, deleteSource } from './sources';
export {
  listAssistants,
  createAssistant,
  getAssistant,
  updateAssistant,
  deleteAssistant,
  publishAssistant,
} from './assistants';
export type { AssistantPageQuery } from './assistants';
export {
  listSchedules,
  createSchedule,
  getSchedule,
  updateSchedule,
  deleteSchedule,
  pauseSchedule,
  resumeSchedule,
  runScheduleNow,
} from './schedules';
export type { SchedulePageQuery } from './schedules';
export { listRuns, getRun } from './runs';
export type { RunPageQuery } from './runs';
export { getCodeRun } from './codeRuns';
export {
  listMcpServers,
  registerMcpServer,
  getMcpServer,
  updateMcpServer,
  deleteMcpServer,
  testMcpServer,
  listMcpServerTools,
} from './mcpServers';
export type { McpPageQuery } from './mcpServers';
export {
  getAccessToken,
  hasAccessToken,
  setAccessToken,
  clearAccessToken,
  subscribeToken,
} from './token';
export { WsClient, parseEnvelope, resolveWsUrl } from './ws';
export type { WsClientOptions, WsConnectionState } from './ws';
export { API_BASE_URL, WS_BASE_URL, DEV_PAGES_ENABLED, parseBoolFlag } from './env';
export type * from './types';
