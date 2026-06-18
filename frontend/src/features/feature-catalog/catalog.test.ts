/**
 * Catalog integrity — the prose↔mechanism guard. Beyond well-formedness, this
 * asserts every in-app `/docs/…` link in the catalog resolves to a REAL bundled
 * doc, so renaming or removing a doc fails this test instead of shipping a dead
 * link on the features page.
 */
import { describe, it, expect } from 'vitest';
import { CATALOG, groupByArea, isInternalLink, type FeatureStatus } from './catalog';
import { DOCS } from '@/features/docs';

const STATUSES: FeatureStatus[] = ['shipped', 'in-progress', 'planned'];

describe('feature catalog', () => {
  it('has well-formed, uniquely-identified entries', () => {
    const ids = new Set<string>();
    for (const e of CATALOG) {
      expect(e.id).toBeTruthy();
      expect(e.title).toBeTruthy();
      expect(e.area).toBeTruthy();
      expect(e.summary.trim().length).toBeGreaterThan(0);
      expect(STATUSES).toContain(e.status);
      expect(ids.has(e.id)).toBe(false);
      ids.add(e.id);
    }
  });

  it('resolves every internal /docs/ link to a real bundled doc (no rot)', () => {
    const slugs = new Set(DOCS.map((d) => d.slug));
    for (const entry of CATALOG) {
      for (const link of entry.links) {
        if (link.href.startsWith('/docs/')) {
          const slug = link.href.slice('/docs/'.length);
          expect(slugs.has(slug), `${entry.id} → ${link.href}`).toBe(true);
        }
      }
    }
  });

  it('groups by area with Platform first', () => {
    const groups = groupByArea(CATALOG);
    expect(groups.length).toBeGreaterThan(0);
    expect(groups[0]?.area).toBe('Platform');
    // Every entry lands in exactly one group.
    const total = groups.reduce((n, g) => n + g.entries.length, 0);
    expect(total).toBe(CATALOG.length);
  });

  it('classifies internal vs external links', () => {
    expect(isInternalLink('/docs/x')).toBe(true);
    expect(isInternalLink('/features')).toBe(true);
    expect(isInternalLink('https://github.com/x')).toBe(false);
  });
});
