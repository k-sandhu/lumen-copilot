/**
 * @vitest-environment node
 *
 * Layout guard for the mid-width clipping fix (issue #246).
 *
 * The app shell is a CSS grid `var(--lc-rail-w) <content>`. A bare `1fr`
 * content track is `minmax(auto, 1fr)`, whose `auto` MINIMUM equals the
 * content's min-content width — so a wide child (the documents data table, a
 * nowrap topbar pill) forces the track past the viewport, and since
 * `.lc-shell__main` is `overflow: hidden`, the excess is clipped with NO
 * scrollbar (unreachable). The fix is `minmax(0, 1fr)`: the track may shrink so
 * children with their own `min-width: 0` + overflow scroll/ellipsis internally.
 *
 * This is pure CSS, so jsdom (no layout engine) can't prove the pixels — this
 * source guard instead asserts the shrinkable track is present on BOTH the
 * default and the collapsed-rail grids, and that no shell grid reintroduces a
 * bare `... 1fr` content track. It fails the moment someone reverts to `1fr`.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const shellCss = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '../routes/shell/shell.css'),
  'utf8',
);

describe('app-shell grid can shrink its content track (#246)', () => {
  it('uses minmax(0, 1fr) for the content column on the default grid', () => {
    expect(shellCss).toMatch(
      /grid-template-columns:\s*var\(--lc-rail-w\)\s+minmax\(0,\s*1fr\)/,
    );
  });

  it('uses minmax(0, 1fr) for the content column on the collapsed-rail grid', () => {
    expect(shellCss).toMatch(
      /grid-template-columns:\s*var\(--lc-rail-w-collapsed\)\s+minmax\(0,\s*1fr\)/,
    );
  });

  it('never reintroduces a bare `<rail> 1fr` content track (the clipping regression)', () => {
    expect(shellCss).not.toMatch(
      /grid-template-columns:\s*var\(--lc-rail-w(?:-collapsed)?\)\s+1fr\s*;/,
    );
  });

  it('bounds the tenant pill label so it ellipsizes instead of widening the topbar', () => {
    // The label must be able to overflow-hide; without this the nowrap tenant
    // id/name is part of the topbar min-content and re-widens the track.
    const tenantBlock = shellCss.slice(shellCss.indexOf('.lc-tenant__name'));
    expect(tenantBlock).toMatch(/text-overflow:\s*ellipsis/);
    expect(tenantBlock).toMatch(/max-width:/);
  });
});
