/**
 * Incremental (per-block) streaming markdown — issue #494.
 *
 * While the chat answer streams, `MarkdownView streaming` splits the accumulated
 * source into SETTLED blocks (each parsed once, memoised, stable content key) plus
 * one TRAILING in-progress block (re-parsed as deltas land). This file pins:
 *   - AC-1: full-pipeline invocations are bounded by (settled blocks + trailing
 *           flushes), NOT by delta count.
 *   - AC-2: a settled block is not re-parsed when later blocks/deltas arrive.
 *   - AC-3: the FINAL streamed DOM is identical to the same text rendered in one
 *           shot — a property test over adversarial split points on nasty docs.
 *   - AC-4 (neg): sanitisation is unchanged on the block-split path (javascript:
 *           links inert, `<script>` stripped, external links keep rel+target).
 *   - AC-6 (neg): links resolve identically across block boundaries.
 *
 * The react-markdown boundary is mocked with a hoisted counter that STILL renders
 * the real pipeline (it wraps `actual.default`), so the same file can both count
 * parses and diff real DOM. `useDeferredValue` is NOT relied on here — the
 * block-split + memo coalescing is deterministic under RTL's synchronous act().
 */
import { createElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// Count every real react-markdown parse and record its source, while still
// rendering the real pipeline so DOM assertions stay honest.
const md = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock('react-markdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-markdown')>();
  const Real = actual.default;
  return {
    ...actual,
    default: (props: import('react-markdown').Options) => {
      md.calls.push(typeof props.children === 'string' ? props.children : '');
      return createElement(Real, props);
    },
  };
});

// Imported AFTER the mock is declared (vi.mock is hoisted above imports anyway).
import { MarkdownView } from './markdown';
import { splitStreamingBlocks } from './markdownBlocks';

function proseHtml(container: HTMLElement): string {
  const el = container.querySelector('.prose-md');
  // Ignore insignificant inter-block whitespace (one-shot keeps the source
  // newlines between block elements; the split path renders each block through
  // its own parser, so those newlines are absent). This normalisation only
  // collapses whitespace BETWEEN tags — a real structural artifact (a split
  // table, a duplicated block) changes tags and is still caught.
  return (el?.innerHTML ?? '').replace(/>\s+</g, '><').trim();
}

beforeEach(() => {
  md.calls.length = 0;
});
afterEach(() => cleanup());

describe('splitStreamingBlocks (the block splitter — #494)', () => {
  it('keeps everything in the trailing block until a following block proves it complete', () => {
    // A lone paragraph being typed is never settled (a setext underline could
    // still turn it into a heading; more text could extend it).
    expect(splitStreamingBlocks('A paragraph')).toEqual({ settled: [], trailing: 'A paragraph' });
    // A trailing blank line does not settle it either (insignificant whitespace is
    // dropped from the trailing region — it renders identically).
    const withBlank = splitStreamingBlocks('A paragraph\n');
    expect(withBlank.settled).toEqual([]);
    expect(withBlank.trailing).toContain('A paragraph');
  });

  it('settles a paragraph once a NON-continuation block follows a blank line', () => {
    const { settled, trailing } = splitStreamingBlocks('First.\n\n## Heading');
    expect(settled).toEqual(['First.']);
    expect(trailing).toBe('## Heading');
  });

  it('never settles an OPEN fenced code block, even across blank lines inside it', () => {
    const src = '```md\n# Inside\n\n- still inside\n';
    const { settled, trailing } = splitStreamingBlocks(src);
    expect(settled).toEqual([]);
    // The blank line inside the fence must NOT split the block.
    expect(trailing).toBe(src);
  });

  it('settles a CLOSED fenced code block when a following block arrives', () => {
    const src = '```js\nconst x = 1;\n```\n\nAfter.';
    const { settled, trailing } = splitStreamingBlocks(src);
    expect(settled).toEqual(['```js\nconst x = 1;\n```']);
    expect(trailing).toBe('After.');
  });

  it('does NOT split a loose list across its blank lines (would change one <ul> into many)', () => {
    // A loose list whose items are blank-separated stays one block while trailing.
    expect(splitStreamingBlocks('- a\n\n- b')).toEqual({ settled: [], trailing: '- a\n\n- b' });
    // …and settles WHOLE once a non-list block follows.
    const { settled, trailing } = splitStreamingBlocks('- a\n\n- b\n\nDone.');
    expect(settled).toEqual(['- a\n\n- b']);
    expect(trailing).toBe('Done.');
  });

  it('keeps an indented list continuation attached to its list item', () => {
    const src = '- item\n\n  continued paragraph\n\nOutside.';
    const { settled, trailing } = splitStreamingBlocks(src);
    expect(settled).toEqual(['- item\n\n  continued paragraph']);
    expect(trailing).toBe('Outside.');
  });

  it('does not split a document containing footnote/reference definitions (safety valve)', () => {
    const src = 'See the note.[^1]\n\nAnother para.\n\n[^1]: the note body.';
    expect(splitStreamingBlocks(src)).toEqual({ settled: [], trailing: src });
    const linkRef = 'Text with [a ref][1].\n\nMore text.\n\n[1]: https://example.com';
    expect(splitStreamingBlocks(linkRef)).toEqual({ settled: [], trailing: linkRef });
  });
});

describe('AC-2 — a settled block is not re-parsed when later blocks/deltas arrive', () => {
  it('never re-parses a settled block, no matter how many later deltas land', () => {
    const A = 'The first paragraph is fully written out.';
    const B = '## A second-level heading';
    // 1) A alone (trailing) 2) A settles, B trailing 3) B settles, tail trailing
    const { rerender } = render(<MarkdownView streaming>{A}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    rerender(<MarkdownView streaming>{`${A}\n\n${B}`}</MarkdownView>);
    rerender(<MarkdownView streaming>{`${A}\n\n${B}\n\nTail`}</MarkdownView>);

    // How many parses so far touched A's / B's content at all. This DISCRIMINATES
    // block-splitting from the whole-document path: the old path re-parses a
    // string CONTAINING A on every flush; the block path parses A standalone,
    // then never again.
    const touchedA = () => md.calls.filter((s) => s.includes(A)).length;
    const touchedB = () => md.calls.filter((s) => s.includes(B)).length;
    const settledA = touchedA();
    const settledB = touchedB();

    // Now grow ONLY the trailing block over many more flushes.
    for (let i = 1; i <= 20; i++) {
      rerender(<MarkdownView streaming>{`${A}\n\n${B}\n\nTail${'!'.repeat(i)}`}</MarkdownView>);
    }
    // The settled blocks were NOT re-parsed by ANY of those 20 flushes (a
    // whole-document reparse would have added 20 to each of these counts).
    expect(touchedA()).toBe(settledA);
    expect(touchedB()).toBe(settledB);
  });
});

describe('AC-1 — pipeline invocations bounded by (settled blocks + trailing flushes), not deltas', () => {
  it('parses one block at a time — no parse ever spans two settled blocks', () => {
    const first = 'Block one is fully done.';
    const middle = '## Two';
    const last = 'Block three is fully done.';
    const prefix = `${first}\n\n${middle}\n\n${last}`; // three blocks
    let source = `${prefix}\n\nTail`;
    const { rerender } = render(<MarkdownView streaming>{source}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    const trailingFlushes = 30;
    for (let i = 1; i <= trailingFlushes; i++) {
      source = `${prefix}\n\nTail word${i}`;
      rerender(<MarkdownView streaming>{source}</MarkdownView>);
    }
    // No single parse contains BOTH the first and last settled block — i.e. the
    // whole document is never re-parsed as one unit (the O(n²) behaviour). On the
    // old path every flush parses a string holding all three blocks.
    const spanningParses = md.calls.filter((s) => s.includes(first) && s.includes(last));
    expect(spanningParses).toHaveLength(0);

    // Total invocations stay bounded by (settled blocks, each parsed ≤ twice:
    // trailing→settle) + one trailing parse per flush — NOT flushes × blocks.
    const settledBlockCount = 3;
    expect(md.calls.length).toBeLessThanOrEqual(settledBlockCount * 2 + (trailingFlushes + 1));
  });
});

// The adversarial corpus: each fed char-by-char (every split point falls inside
// some construct), then the FINAL streamed DOM is diffed against the one-shot
// render. A premature settle would leave a stale, content-keyed block in the DOM
// and this comparison would fail.
const NASTY: Record<string, string> = {
  gfmTable: '| Col A | Col B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\nAfter the table.',
  fencedCodeWithMarkdown:
    '```md\n# Heading in code\n\n- a list item\n- another\n```\n\nProse after.',
  nestedList: '- top level\n  - nested one\n  - nested two\n- second top\n\nDone.',
  looseList: '1. first item\n\n2. second item\n\n3. third item\n\nEnd of list.',
  blockquote: '> quoted line one\n> quoted line two\n\nOutside the quote.',
  setextHeadings: 'Big Title\n=========\n\nA paragraph.\n\nSub Heading\n---\n\nBody text here.',
  mixed:
    '# Title\n\nIntro paragraph.\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n```py\nprint("hi")\n```\n\nClosing words.',
  footnote: 'A claim needing a source.[^ref]\n\nA middle paragraph.\n\n[^ref]: the citation body.',
};

describe('AC-3 — final streamed DOM equals the one-shot DOM (adversarial split points)', () => {
  for (const [name, doc] of Object.entries(NASTY)) {
    it(`streams "${name}" delta-by-delta to a DOM identical to one shot`, () => {
      const oneShot = render(<MarkdownView>{doc}</MarkdownView>, { wrapper: MemoryRouter });
      const expected = proseHtml(oneShot.container);
      oneShot.unmount();

      // Feed char-by-char through the streaming path.
      const stream = render(<MarkdownView streaming>{doc.slice(0, 1)}</MarkdownView>, {
        wrapper: MemoryRouter,
      });
      for (let k = 2; k <= doc.length; k++) {
        stream.rerender(<MarkdownView streaming>{doc.slice(0, k)}</MarkdownView>);
      }
      expect(proseHtml(stream.container)).toBe(expected);
    });
  }
});

describe('AC-4 / AC-6 (negative) — sanitisation + links identical on the block-split path', () => {
  it('strips a raw <script> in a settled block (no raw HTML escapes the sanitizer)', () => {
    const src = 'Intro paragraph.\n\nHello <script>alert(1)</script> world\n\ntail';
    const { container } = render(<MarkdownView streaming>{src}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    expect(container.querySelector('script')).toBeNull();
  });

  it('does not render a javascript: link href on the streaming path', () => {
    const src = 'A line.\n\n[click me](javascript:alert(1))\n\ntail';
    const { container } = render(<MarkdownView streaming>{src}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    const anchors = Array.from(container.querySelectorAll('a'));
    for (const a of anchors) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^javascript:/i);
    }
  });

  it('keeps external links safe (target=_blank, rel=noreferrer noopener) across blocks', () => {
    const src = 'Some intro text.\n\nSee [the site](https://example.com/page) for more.\n\ntail';
    const { container } = render(<MarkdownView streaming>{src}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    const link = container.querySelector('a[href="https://example.com/page"]');
    expect(link).not.toBeNull();
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer noopener');
  });

  it('resolves an internal link identically inside a settled block (AC-6)', () => {
    const resolve = (href: string) => (href === 'https://docs/x' ? '/docs/x' : null);
    const src = 'Intro.\n\nGo to [the doc](https://docs/x) now.\n\ntail';
    const { container } = render(
      <MarkdownView streaming resolveInternalLink={resolve}>
        {src}
      </MarkdownView>,
      { wrapper: MemoryRouter },
    );
    // resolveInternalLink → react-router <Link to="/docs/x"> (an <a href="/docs/x">).
    const link = container.querySelector('a[href="/docs/x"]');
    expect(link).not.toBeNull();
    expect(link).not.toHaveAttribute('target');
  });

  it('keeps inline citation markers ([n]) as literal text across block boundaries (AC-6)', () => {
    // Chat renders citation markers as literal `[n]` text inside the answer body
    // (the numbered chips live in a separate list). The block split must not tear a
    // marker or alter its rendering — the streamed body equals the one-shot body.
    const src = 'The revenue rose [1] last quarter.\n\nMargins held steady [2] as well.';
    const oneShot = render(<MarkdownView>{src}</MarkdownView>, { wrapper: MemoryRouter });
    const expected = proseHtml(oneShot.container);
    oneShot.unmount();

    const stream = render(<MarkdownView streaming>{src.slice(0, 1)}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    for (let k = 2; k <= src.length; k++) {
      stream.rerender(<MarkdownView streaming>{src.slice(0, k)}</MarkdownView>);
    }
    expect(proseHtml(stream.container)).toBe(expected);
    // The literal markers survive verbatim in the streamed body.
    expect(stream.container.textContent).toContain('[1]');
    expect(stream.container.textContent).toContain('[2]');
  });
});
