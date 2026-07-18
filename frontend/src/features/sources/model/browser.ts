/**
 * The one full-page-navigation seam of the Sources slice (#455, ADR-0019 §1).
 * The OAuth consent flow leaves the SPA entirely: the browser navigates to the
 * provider's `authorization_url` and later returns via the backend callback's
 * 302 onto /sources. Kept as a module so components stay testable (tests mock
 * this seam; jsdom cannot perform real navigation).
 */

/** Navigate the WHOLE browser (not the SPA router) to the provider consent URL. */
export function navigateToConsent(url: string): void {
  window.location.assign(url);
}
