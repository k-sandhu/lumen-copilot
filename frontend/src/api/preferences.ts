/**
 * Typed preferences calls — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA performs preferences HTTP. Conforms to the frozen contract
 * (contracts/openapi.yaml §preferences, spec 0005):
 *
 *   GET   /preferences → UserPreferences   (the caller's; fresh user → nulls)
 *   PATCH /preferences → UserPreferences   (set/clear the default model)
 *
 * Always the caller's row (no id in the path); an unknown `default_model_id` on
 * PATCH is a 422 surfaced as an ApiError the caller branches on.
 */
import { request } from './client';
import type { UserPreferences, UserPreferencesUpdate } from './types';

/** The caller's account preferences (a fresh user gets null fields, no write). */
export function getPreferences(signal?: AbortSignal): Promise<UserPreferences> {
  return request<UserPreferences>('/preferences', { signal });
}

/** Set or clear the caller's preferences (e.g. the default chat model). */
export function updatePreferences(body: UserPreferencesUpdate): Promise<UserPreferences> {
  return request<UserPreferences>('/preferences', { method: 'PATCH', json: body });
}
