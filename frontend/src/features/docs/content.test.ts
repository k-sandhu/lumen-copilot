/**
 * Proves the docs actually bundle into the SPA via `import.meta.glob('?raw')` —
 * i.e. the outside-root glob resolves under Vite/Vitest. This is the load-bearing
 * de-risk for the whole docs viewer (no backend serves these files).
 */
import { describe, it, expect } from 'vitest';
import { DOCS } from './content';
import { buildPathIndex } from './model';

describe('docs content bundling (import.meta.glob ?raw)', () => {
  it('bundles many markdown files from docs/ and the contracts', () => {
    expect(DOCS.length).toBeGreaterThan(15);
  });

  it('derives slug/group/path/title for a known ADR', () => {
    const adr = DOCS.find((d) => d.slug === 'architecture/0003-application-stack');
    expect(adr).toBeDefined();
    expect(adr?.group).toBe('architecture');
    expect(adr?.path).toBe('docs/architecture/0003-application-stack.md');
    expect((adr?.title.length ?? 0) > 0).toBe(true);
  });

  it('includes the top-level agent contract, grouped under Contracts', () => {
    const agents = DOCS.find((d) => d.slug === 'AGENTS');
    expect(agents).toBeDefined();
    expect(agents?.group).toBe('Contracts');
  });

  it('has non-empty content and unique paths for every entry', () => {
    for (const d of DOCS) expect(d.content.trim().length).toBeGreaterThan(0);
    expect(Object.keys(buildPathIndex(DOCS)).length).toBe(DOCS.length);
  });
});
