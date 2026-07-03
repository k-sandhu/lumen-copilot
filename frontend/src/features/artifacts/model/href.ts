/**
 * The one place the artifacts slice maps an artifact id to an in-app href (#222).
 *
 * The code-run inspector (#232) renders each produced artifact id as an `<a href>`
 * chip via a `resolveArtifactHref` callback. It must be a plain, browser-navigable
 * URL — but the artifact *content* endpoint is `bearerAuth` (a raw `<a href>` GET
 * can't attach the token, and a token in a URL would leak). So a chip does NOT
 * link straight to the bytes; it links to the in-app artifacts route, deep-linked
 * to that artifact, where the panel loads + previews + downloads it through the
 * api/ boundary (bearer attached). This keeps the token off every URL while still
 * turning the inert id chips into real, clickable links to the file.
 */

/** The route path the artifacts panel is mounted at. */
export const ARTIFACTS_ROUTE = '/artifacts';

/** The query-param the panel reads to deep-link/focus a specific artifact. */
export const ARTIFACT_PARAM = 'artifact';

/**
 * Resolve an artifact id to its in-app deep link (`/artifacts?artifact=<id>`).
 *
 * Suitable as a `resolveArtifactHref` for `CodeRunInspector` / `CodeRunPanel` /
 * `CodeRunDetail`: it turns an inert artifact-id chip into a real link that opens
 * the artifact panel focused on that file. Never returns a token-bearing URL.
 */
export function artifactHref(artifactId: string): string {
  return `${ARTIFACTS_ROUTE}?${ARTIFACT_PARAM}=${encodeURIComponent(artifactId)}`;
}
