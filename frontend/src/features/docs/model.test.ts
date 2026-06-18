/**
 * Pure unit tests for the docs model: slug/group/title derivation, grouping/order,
 * and the in-app link resolver. No bundler involved — fed a synthetic map.
 */
import { describe, it, expect } from 'vitest';
import { parseDocs, groupDocs, buildPathIndex, resolveDocLink } from './model';

const RAW: Record<string, string> = {
  'docs/architecture/0003-application-stack.md': '# Application stack\n\nADR body.',
  'docs/architecture/0004-architecture-boundaries-and-adapters.md':
    '# Boundaries\n\nSee ../specs/0001-open-decisions.md',
  'docs/architecture/README.md': '# Architecture Decision Records\n',
  'docs/specs/0001-open-decisions.md': '# Open decisions\n',
  'docs/WORK_TRACKING.md': '# Work tracking\n',
  'AGENTS.md': '# AGENTS — contract\n\nlinks docs/specs/0001-open-decisions.md',
  'backend/AGENTS.md': '# backend contract\n',
  'README.md': '# Beacon\n',
};

function getBySlug(docs: ReturnType<typeof parseDocs>, slug: string) {
  const d = docs.find((x) => x.slug === slug);
  if (!d) throw new Error(`expected a doc with slug ${slug}`);
  return d;
}

describe('parseDocs', () => {
  const docs = parseDocs(RAW);

  it('derives slug, group, and title from path + first heading', () => {
    expect(getBySlug(docs, 'architecture/0003-application-stack').title).toBe('Application stack');
    expect(getBySlug(docs, 'architecture/0003-application-stack').group).toBe('architecture');
    expect(getBySlug(docs, 'WORK_TRACKING').group).toBe('General');
    expect(getBySlug(docs, 'AGENTS').group).toBe('Contracts');
    expect(getBySlug(docs, 'backend/AGENTS').group).toBe('Contracts');
    expect(getBySlug(docs, 'README').group).toBe('Repo');
  });

  it('falls back to the filename when there is no H1', () => {
    const noHeading = parseDocs({ 'docs/x/no-heading.md': 'just text, no h1' });
    expect(noHeading[0]?.title).toBe('no-heading');
  });
});

describe('groupDocs', () => {
  it('orders groups by GROUP_ORDER and floats READMEs to the top of a group', () => {
    const groups = groupDocs(parseDocs(RAW));
    const keys = groups.map((g) => g.key);
    expect(keys.indexOf('Contracts')).toBeLessThan(keys.indexOf('architecture'));
    expect(keys.indexOf('architecture')).toBeLessThan(keys.indexOf('specs'));
    const arch = groups.find((g) => g.key === 'architecture');
    expect(arch?.entries[0]?.slug).toBe('architecture/README');
  });
});

describe('resolveDocLink', () => {
  const index = buildPathIndex(parseDocs(RAW));

  it('resolves a relative ../ link between docs to its route', () => {
    expect(
      resolveDocLink(
        'docs/architecture/0004-architecture-boundaries-and-adapters.md',
        '../specs/0001-open-decisions.md',
        index,
      ),
    ).toBe('/docs/specs/0001-open-decisions');
  });

  it('resolves a repo-root-relative link from a top-level contract', () => {
    expect(resolveDocLink('AGENTS.md', 'docs/specs/0001-open-decisions.md', index)).toBe(
      '/docs/specs/0001-open-decisions',
    );
  });

  it('strips anchors/queries before matching', () => {
    expect(
      resolveDocLink('AGENTS.md', 'docs/specs/0001-open-decisions.md#section-2', index),
    ).toBe('/docs/specs/0001-open-decisions');
  });

  it('returns null for external, anchor-only, unknown, and missing links', () => {
    expect(resolveDocLink('AGENTS.md', 'https://example.com', index)).toBeNull();
    expect(resolveDocLink('AGENTS.md', '#section', index)).toBeNull();
    expect(resolveDocLink('AGENTS.md', 'backend/app/llm/', index)).toBeNull();
    expect(resolveDocLink('AGENTS.md', undefined, index)).toBeNull();
  });
});
