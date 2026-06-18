/**
 * Typed model-picker call — part of the api/ boundary. Conforms to the frozen
 * contract (contracts/openapi.yaml §models): the curated chat models the UI
 * offers, grouped by tier (frontier / fast / oss), exactly one `is_default`.
 */
import { request } from './client';
import type { ModelList } from './types';

/** The selectable chat models (picker registry). */
export function listModels(signal?: AbortSignal): Promise<ModelList> {
  return request<ModelList>('/models', { signal });
}
