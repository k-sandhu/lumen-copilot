/**
 * Typed search call — part of the api/ boundary (ADR-0004). The ONLY place the
 * SPA performs search HTTP. Conforms to the FROZEN contract (contracts/openapi.yaml
 * §search, M2 #80):
 *
 *   GET /search?q&collection_id&source&type&cursor&limit → SearchResponse
 *
 * The results are permission-trimmed and ranked: they contain ONLY passages in
 * the caller's effective allow-set within their tenant (spec 0004 INV-1/INV-2).
 * Passages the caller may not see are never returned — they are counted in
 * `hidden_count` so the UI can disclose the trim without leaking content. When
 * the permitted results answer the query, `direct_answer` carries a cited
 * synthesized answer whose every claim references a passage present in `results`
 * (INV-3); otherwise it is omitted (prefer "no answer" over an unsourced claim).
 *
 * Negative paths (spec 0004): missing/expired token → 401 (INV-4); wrong context
 * → 403; a malformed query (e.g. empty `q`) → 422 (INV-8). Each surfaces as a
 * typed `ApiError` carrying the Problem body the UI branches on.
 */
import { request } from './client';
import type { SearchQuery, SearchResponse } from './types';

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/**
 * Permission-trimmed ranked search, with an optional cited direct answer.
 * `q` is required; `collection_id` / `source` / `type` narrow the result set;
 * `cursor` / `limit` page through it.
 */
export function search(query: SearchQuery, signal?: AbortSignal): Promise<SearchResponse> {
  const qs = buildQuery({
    q: query.q,
    collection_id: query.collection_id,
    source: query.source,
    type: query.type,
    cursor: query.cursor,
    limit: query.limit,
  });
  return request<SearchResponse>(`/search${qs}`, { signal });
}
