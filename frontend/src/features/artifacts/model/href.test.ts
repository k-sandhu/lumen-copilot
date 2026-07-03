/**
 * The artifact-id → in-app href helper (#222). The code-run inspector's inert
 * artifact chips become real links via this: they point at the artifacts panel,
 * deep-linked to the file, NOT at the bearer-only content endpoint (no token in
 * any URL).
 */
import { describe, it, expect } from 'vitest';
import { artifactHref, ARTIFACTS_ROUTE, ARTIFACT_PARAM } from './href';

describe('artifactHref', () => {
  it('deep-links the artifacts panel to a specific artifact', () => {
    expect(artifactHref('art-1')).toBe(`${ARTIFACTS_ROUTE}?${ARTIFACT_PARAM}=art-1`);
  });

  it('never points at the bearer-only content endpoint (no token leak)', () => {
    const href = artifactHref('art-1');
    expect(href).not.toContain('/content');
    expect(href).not.toContain('token');
    expect(href.startsWith(ARTIFACTS_ROUTE)).toBe(true);
  });

  it('url-encodes the id', () => {
    expect(artifactHref('a b')).toContain('a%20b');
  });
});
