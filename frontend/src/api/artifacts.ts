/**
 * Typed artifact calls — part of the api/ boundary (ADR-0004). The ONLY place the
 * SPA performs HTTP for the artifacts slice; features consume hooks built on
 * these, never `fetch`/`XMLHttpRequest` directly.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §artifacts, CC-B #208 /
 * panel #222):
 *   GET    /artifacts?session_id&produced_by&cursor&limit → ArtifactList
 *   GET    /artifacts/{id}                                 → Artifact
 *   DELETE /artifacts/{id}                                 → 204
 *   GET    /artifacts/{id}/content                         → 200 bytes | 302 presigned URL
 *
 * Artifacts are files an agent/run PRODUCED (the write_file tool #220, the code
 * sandbox #230/#231) — distinct from uploaded `documents`, and there is
 * deliberately no create endpoint (agents write through the backend service seam).
 *
 * Negative paths honored (spec 0004): a cross-tenant / non-owned / unknown id
 * surfaces as a typed 404 ApiError (INV-1/INV-2), never 403; a malformed id → 422
 * (INV-8); a missing/expired token → 401 (INV-4). The client surfaces the typed
 * Problem body so the UI branches on shape, not status text.
 */
import { request, ApiError, withRefreshRetry } from './client';
import { API_BASE_URL } from './env';
import { getAccessToken } from './token';
import type { Artifact, ArtifactList, ArtifactListQuery, Problem } from './types';

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

function joinApi(path: string): string {
  const b = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

/** Coerce a parsed body into a Problem when it has the required shape. */
function problemFrom(value: unknown): Problem | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const v = value as Record<string, unknown>;
  if (typeof v.title === 'string' && typeof v.status === 'number') return value as Problem;
  return undefined;
}

// --- List / get / delete ----------------------------------------------------

/** List the caller's produced artifacts, optionally filtered by session / origin. */
export function listArtifacts(
  query: ArtifactListQuery = {},
  signal?: AbortSignal,
): Promise<ArtifactList> {
  const qs = buildQuery({
    session_id: query.session_id,
    produced_by: query.produced_by,
    cursor: query.cursor,
    limit: query.limit,
  });
  return request<ArtifactList>(`/artifacts${qs}`, { signal });
}

/** Get one artifact's metadata; a non-owned / cross-tenant / unknown id → 404. */
export function getArtifact(id: string, signal?: AbortSignal): Promise<Artifact> {
  return request<Artifact>(`/artifacts/${id}`, { signal });
}

/** Delete an artifact (stored object + row); a non-owned / unknown id → 404 (204 on success). */
export function deleteArtifact(id: string): Promise<void> {
  return request<void>(`/artifacts/${id}`, { method: 'DELETE' });
}

// --- Content (download / preview) -------------------------------------------

/** A loadable artifact content source, plus a revoke for the object URL we minted. */
export interface ArtifactContent {
  /** `blob:` URL to point a download `<a>` / preview `<img>` / text reader at. */
  url: string;
  /** The blob's content type (carried through the presigned/inline response). */
  type: string;
  /** Release the underlying object URL. */
  revoke: () => void;
}

/**
 * Load a permitted artifact's bytes for download or inline preview (AC-1/AC-2).
 *
 * INV-4 / contract: `GET /artifacts/{id}/content` is a `bearerAuth` endpoint —
 * authorized by the in-memory access JWT, not a cookie. A browser-initiated
 * `<a href>` / `<img src>` GET cannot attach an `Authorization: Bearer` header, so
 * it would 401. We therefore fetch the bytes here with the bearer attached and
 * hand back a `blob:` object URL the UI can safely use (download or preview),
 * never leaking the access token into an anchor/image URL.
 *
 * The endpoint may instead 302 to a short-TTL presigned URL (CC-12). We issue a
 * `redirect: 'follow'` fetch so the browser transparently follows that redirect
 * and streams the bytes; the resulting body is still turned into a blob URL. A 404
 * (cross-tenant / not permitted, INV-2) and a 401 surface as a typed ApiError
 * carrying the Problem body — the caller renders an error + retry, never a dead end.
 */
export async function fetchArtifactContent(
  artifactId: string,
  signal?: AbortSignal,
): Promise<ArtifactContent> {
  const url = joinApi(`/artifacts/${artifactId}/content`);
  return withRefreshRetry(async () => {
    const headers = new Headers({ Accept: 'application/octet-stream, application/problem+json' });
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);

    let response: Response;
    try {
      // credentials:'include' lets the refresh cookie ride along, but the actual
      // authorization is the bearer header above; redirect:'follow' transparently
      // chases the contract's optional 302 → presigned URL.
      response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        redirect: 'follow',
        headers,
        signal,
      });
    } catch (cause) {
      throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
    }

    if (!response.ok) {
      let problem: Problem | undefined;
      try {
        problem = problemFrom(await response.json());
      } catch {
        /* non-JSON body */
      }
      throw new ApiError(
        `Content request for ${artifactId} failed with ${response.status}`,
        response.status,
        problem,
      );
    }

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    return { url: objectUrl, type: blob.type, revoke: () => URL.revokeObjectURL(objectUrl) };
  });
}
