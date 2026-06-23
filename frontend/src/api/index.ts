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
  resolveDocumentContentUrl,
  fetchDocumentContent,
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
export { listAuditEvents } from './audit';
export { listMembers, getModelGovernance, getRiskTiers } from './admin';
export {
  getAccessToken,
  hasAccessToken,
  setAccessToken,
  clearAccessToken,
  subscribeToken,
} from './token';
export { WsClient, parseEnvelope, resolveWsUrl } from './ws';
export type { WsClientOptions, WsConnectionState } from './ws';
export { API_BASE_URL, WS_BASE_URL } from './env';
export type * from './types';
