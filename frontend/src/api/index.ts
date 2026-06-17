/**
 * Public surface of the api/ boundary. Import from here, not from deep paths.
 * This is the ONLY module the rest of the app uses to reach the backend.
 */
export { ApiError, request } from './client';
export type { RequestOptions } from './client';
export { getHealth, getReadiness } from './health';
export { WsClient, parseEnvelope, resolveWsUrl } from './ws';
export type { WsClientOptions, WsConnectionState } from './ws';
export { API_BASE_URL, WS_BASE_URL } from './env';
export type * from './types';
