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
  // AC-3 is a BYTE-identity claim, so we compare the UNMODIFIED `.prose-md`
  // innerHTML — no whitespace normalisation (FE-8). The earlier version stripped
  // inter-tag whitespace (`>\s+<` → `><`), which made `</p>\n<h2>` and `</p><h2>`
  // compare equal and so silently masked any artifact that surfaced only as
  // changed whitespace between blocks. Exact separators are now reproduced on the
  // split path itself: a one-shot parse joins top-level blocks with a single `\n`
  // text node, and `StreamingMarkdownBody` re-inserts exactly that `\n` between
  // adjacent blocks — so the two DOMs are genuinely, byte-for-byte identical and a
  // premature settle / split / duplicated block (which changes tags OR the
  // whitespace between them) now fails this comparison.
  return container.querySelector('.prose-md')?.innerHTML ?? '';
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

  it('does NOT treat a fence-prefixed line with trailing text as a close (FE-6)', () => {
    // CommonMark: a closing fence carries NO info string — only spaces/tabs may
    // follow the run. `\`\`\`not-a-close` is therefore CONTENT inside the block, not
    // a close, so the block stays open until a bare `\`\`\`` arrives.
    const stillOpen = splitStreamingBlocks('```js\ncode\n```not-a-close\nmore');
    expect(stillOpen.settled).toEqual([]);
    expect(stillOpen.trailing).toBe('```js\ncode\n```not-a-close\nmore');

    // …and once the REAL bare close + a following block arrive, the ENTIRE fence
    // (false close included) settles as a single code block — never split in two.
    const src = '```js\nconst x = 1;\n```not-a-close\nstill code\n```\n\nAfter.';
    const { settled, trailing } = splitStreamingBlocks(src);
    expect(settled).toEqual(['```js\nconst x = 1;\n```not-a-close\nstill code\n```']);
    expect(trailing).toBe('After.');

    // A bare close with only trailing whitespace still closes (spaces/tabs are ok).
    const wsClose = splitStreamingBlocks('```\ncode\n```  \t\n\nAfter.');
    expect(wsClose.settled).toEqual(['```\ncode\n```  \t']);
    expect(wsClose.trailing).toBe('After.');
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

  it('does NOT split an indented code block on its internal blank lines (would make two <pre>s)', () => {
    // A CommonMark indented code block (≥4 spaces) absorbs the blank line between
    // its lines — one-shot renders ONE <pre>, so the split must keep it whole.
    const src = 'Intro.\n\n    code line 1\n\n    code line 2\n\nAfter.';
    const { settled, trailing } = splitStreamingBlocks(src);
    expect(settled).toEqual(['Intro.', '    code line 1\n\n    code line 2']);
    expect(trailing).toBe('After.');
    // A non-indented line after the blank still ends the code block.
    const ended = splitStreamingBlocks('    only code\n\nplain paragraph');
    expect(ended.settled).toEqual(['    only code']);
    expect(ended.trailing).toBe('plain paragraph');
  });

  it('does not split a document containing footnote/reference definitions (safety valve)', () => {
    const src = 'See the note.[^1]\n\nAnother para.\n\n[^1]: the note body.';
    expect(splitStreamingBlocks(src)).toEqual({ settled: [], trailing: src });
    const linkRef = 'Text with [a ref][1].\n\nMore text.\n\n[1]: https://example.com';
    expect(splitStreamingBlocks(linkRef)).toEqual({ settled: [], trailing: linkRef });
  });

  it('never settles inside a multiline HTML block (CommonMark conditions 1–5) — FE2-1', () => {
    // CommonMark HTML block start conditions 1–5 END on a TERMINATOR STRING, not a
    // blank line, so such a block legally CONTAINS blank lines. Splitting on one
    // tears the block: one-shot keeps `<!-- a\n\nb -->` whole (raw ⇒ dropped),
    // while the split exposes `b -->` as a visible paragraph — a DOM divergence
    // AND a leak of hidden source text into the rendered answer.
    expect(splitStreamingBlocks('Intro.\n\n<!-- a\n\nb -->\n\nAfter.')).toEqual({
      settled: ['Intro.', '<!-- a\n\nb -->'],
      trailing: 'After.',
    });
    // While the block is still OPEN nothing after it can settle, ever.
    expect(splitStreamingBlocks('Intro.\n\n<!-- a\n\nb\n\nc')).toEqual({
      settled: ['Intro.'],
      trailing: '<!-- a\n\nb\n\nc',
    });
    // Condition 1: <script>/<style>/<pre>/<textarea> end only at their close tag.
    expect(splitStreamingBlocks('<script>\nx\n\ny\n</script>\n\nAfter.')).toEqual({
      settled: ['<script>\nx\n\ny\n</script>'],
      trailing: 'After.',
    });
    // Condition 3: a processing instruction ends at `?>`.
    expect(splitStreamingBlocks('<?php\n$a = 1;\n\n$b = 2;\n?>\n\nAfter.')).toEqual({
      settled: ['<?php\n$a = 1;\n\n$b = 2;\n?>'],
      trailing: 'After.',
    });
    // The end condition may already be met on the START line — a one-line comment
    // is complete there, so the following blank settles it as usual.
    expect(splitStreamingBlocks('<!-- one line -->\n\nAfter.')).toEqual({
      settled: ['<!-- one line -->'],
      trailing: 'After.',
    });
    // Conditions 6/7 (`<div>`, `<table>`, a bare complete tag) DO end at a blank
    // line — one-shot splits there too, so the existing boundary stays.
    expect(splitStreamingBlocks('<div>\n\ntext\n\n</div>\n\nAfter.')).toEqual({
      settled: ['<div>', 'text', '</div>'],
      trailing: 'After.',
    });
  });

  it('keeps only the SUFFIX from the first cross-block reference trailing — FE2-2', () => {
    // A citation marker (`[1]`) reads as a shortcut reference USE, so the round-1
    // valve kept the WHOLE answer trailing — incremental parsing was effectively
    // off for every grounded answer. The valve is now surgical: the already-safe
    // settled PREFIX is preserved and only the suffix from the first offending
    // block stays trailing.
    expect(
      splitStreamingBlocks('Para one.\n\nPara two [1] with a citation.\n\nPara three.'),
    ).toEqual({
      settled: ['Para one.'],
      trailing: 'Para two [1] with a citation.\n\nPara three.',
    });
    // A block with no reference use settles even though a LATER block has one.
    expect(splitStreamingBlocks('Alpha.\n\nBeta.\n\nGamma [x].\n\nDelta.')).toEqual({
      settled: ['Alpha.', 'Beta.'],
      trailing: 'Gamma [x].\n\nDelta.',
    });
    // A reference/footnote DEFINITION trips too (a use in a LATER block must be
    // parsed together with it, or the settled render would show literal `[x]`).
    expect(splitStreamingBlocks('Alpha.\n\nBeta.\n\n[x]: https://e.com')).toEqual({
      settled: ['Alpha.', 'Beta.'],
      trailing: '[x]: https://e.com',
    });
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

describe('FE-7 — a late reference definition never un-settles an earlier block', () => {
  // A reference-style USE (`[text][id]`, or the shortcut `[x]`) can appear and let
  // earlier blocks settle; when the matching DEFINITION lands in a LATER delta the
  // old safety valve suddenly tripped and collapsed the whole document to one
  // trailing block — remounting & reparsing every already-settled block (a visible
  // flash, defeating AC-2), and turning the use into a link so the settled render no
  // longer matched one-shot. The valve now trips on the USE itself, so nothing
  // settles while an unresolved reference is outstanding.
  const doc = 'Uses a ref [x].\n\nSecond para.\n\n[x]: https://e.com';

  it('keeps the settled set APPEND-ONLY across every delta (no un-settle / remount)', () => {
    // The settled array is the source of the React content-keys; if it is
    // append-only (each step keeps the previous blocks as an ordered prefix and
    // only grows), no settled block can change key ⇒ none remounts.
    let prev: string[] = [];
    for (let k = 1; k <= doc.length; k++) {
      const { settled } = splitStreamingBlocks(doc.slice(0, k));
      expect(settled.length).toBeGreaterThanOrEqual(prev.length);
      expect(settled.slice(0, prev.length)).toEqual(prev);
      prev = settled;
    }
    // With the reference use outstanding the whole source stays trailing, so no
    // block ever settles — the strongest possible guarantee against un-settling.
    expect(prev).toEqual([]);
  });

  it('streams to a DOM identical to one shot despite the cross-block reference', () => {
    const oneShot = render(<MarkdownView>{doc}</MarkdownView>, { wrapper: MemoryRouter });
    const expected = proseHtml(oneShot.container);
    oneShot.unmount();

    const stream = render(<MarkdownView streaming>{doc.slice(0, 1)}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    for (let k = 2; k <= doc.length; k++) {
      stream.rerender(<MarkdownView streaming>{doc.slice(0, k)}</MarkdownView>);
    }
    expect(proseHtml(stream.container)).toBe(expected);
    // The definition resolved the use into a real link (proving the reference is
    // genuinely cross-block, so settling `Uses a ref [x].` alone would have been
    // WRONG — it would have rendered a literal `[x]`).
    expect(stream.container.querySelector('a[href="https://e.com"]')).not.toBeNull();
  });
});

describe('FE2-2 — the cross-block-reference valve is surgical and append-only', () => {
  const CITATION = 'Para one.\n\nPara two [1] with a citation.\n\nPara three.';
  const REF_LINK = 'Uses [a][1].\n\nMiddle.\n\n[1]: https://e.com';
  const RETRACTION = 'First.\n\nSecond [x]';

  function streamTo(doc: string) {
    const stream = render(<MarkdownView streaming>{doc.slice(0, 1)}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    for (let k = 2; k <= doc.length; k++) {
      stream.rerender(<MarkdownView streaming>{doc.slice(0, k)}</MarkdownView>);
    }
    return stream;
  }

  it('(a) still parses an answer carrying [n] citation markers INCREMENTALLY', () => {
    // Round 1 collapsed this to one giant trailing block (every grounded answer
    // carries `[n]` markers), which disabled incremental parsing in the common case.
    const { settled, trailing } = splitStreamingBlocks(CITATION);
    expect(settled).toEqual(['Para one.']);
    expect(trailing).toBe('Para two [1] with a citation.\n\nPara three.');

    const oneShot = render(<MarkdownView>{CITATION}</MarkdownView>, { wrapper: MemoryRouter });
    const expected = proseHtml(oneShot.container);
    oneShot.unmount();
    expect(proseHtml(streamTo(CITATION).container)).toBe(expected);
  });

  it('(b) still renders a genuine reference link resolved by its later definition', () => {
    const oneShot = render(<MarkdownView>{REF_LINK}</MarkdownView>, { wrapper: MemoryRouter });
    const expected = proseHtml(oneShot.container);
    oneShot.unmount();

    const stream = streamTo(REF_LINK);
    expect(proseHtml(stream.container)).toBe(expected);
    // The definition really did resolve the use into a link — so settling
    // `Uses [a][1].` alone would have rendered literal text instead.
    expect(stream.container.querySelector('a[href="https://e.com"]')).not.toBeNull();
  });

  it('(c) never un-settles a block that already settled at an earlier delta', () => {
    // `First.` settles, then the arriving `]` completed a shortcut reference and the
    // round-1 GLOBAL valve retracted the settled set from one block to zero — a key
    // change ⇒ a remount + reparse of already-rendered content.
    for (const doc of [CITATION, REF_LINK, RETRACTION]) {
      let prev: string[] = [];
      for (let k = 1; k <= doc.length; k++) {
        const { settled } = splitStreamingBlocks(doc.slice(0, k));
        expect(settled.slice(0, prev.length), `delta ${k} of ${JSON.stringify(doc)}`).toEqual(prev);
        prev = settled;
      }
    }
    // …and the concrete retraction from the finding: `First.` stays settled.
    expect(splitStreamingBlocks('First.\n\nSecond [x').settled).toEqual(['First.']);
    expect(splitStreamingBlocks(RETRACTION).settled).toEqual(['First.']);
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
  indentedCode: 'Intro paragraph.\n\n    code line 1\n\n    code line 2\n\nAfter the code block.',
  setextHeadings: 'Big Title\n=========\n\nA paragraph.\n\nSub Heading\n---\n\nBody text here.',
  mixed:
    '# Title\n\nIntro paragraph.\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n```py\nprint("hi")\n```\n\nClosing words.',
  footnote: 'A claim needing a source.[^ref]\n\nA middle paragraph.\n\n[^ref]: the citation body.',
  // FE-6: a fence-prefixed line with trailing text (```not-a-close) is NOT a close;
  // the whole fence (through the bare ```) must be ONE <pre>, matching one-shot.
  fenceFalseClose: '```js\nconst x = 1;\n```not-a-close\nstill code\n```\n\nAfter the code.',
  // FE-6 (DOM-visible at the FINAL frame): the fence has a false close and NO real
  // close, so the unclosed fence absorbs the blank line and `After.` (one <pre>). A
  // splitter that wrongly treats `\`\`\`not-a-close` as a close would settle a
  // premature code block and render `After.` as a stray paragraph — a different DOM.
  // (The FE-7 cross-block-reference DOM is covered by its own describe block below.)
  fenceFalseCloseUnclosed: '```js\ncode\n```not-a-close\nmore code\n\nAfter.',
  // FE2-1: CommonMark HTML blocks whose END condition is a terminator string
  // (start conditions 1–5) may CONTAIN blank lines. One-shot keeps each whole (raw
  // HTML is dropped by the pipeline, so nothing of it is rendered); a splitter that
  // settles on the internal blank line exposes the block's tail as a paragraph.
  htmlCommentBlankLine:
    'Before the note.\n\n<!-- hidden note line one\n\nhidden note line two -->\n\nAfter the note.',
  htmlScriptBlankLine: 'Intro.\n\n<script>\nvar a = 1;\n\nvar b = 2;\n</script>\n\nOutro.',
  htmlPreBlankLine: 'Intro.\n\n<pre>\nline one\n\nline two\n</pre>\n\nOutro.',
  htmlStyleBlankLine:
    'Intro.\n\n<style>\n.a { color: red; }\n\n.b { color: blue; }\n</style>\n\nOutro.',
  // …while conditions 6/7 (`<div>`, `<table>`) DO end at a blank line, so one-shot
  // splits there too — the existing boundary must keep matching.
  htmlDivBlankLine: 'Intro.\n\n<div class="wrap">\n\nSome *emphasis* inside.\n\n</div>\n\nOutro.',
  htmlTableBlankLine:
    'Intro.\n\n<table>\n<tr><td>a</td></tr>\n\n<tr><td>b</td></tr>\n</table>\n\nOutro.',
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

  it('does not execute or leak a multiline <script> HTML block on the block path (FE2-1)', () => {
    // The block spans a blank line. A splitter that settles there renders the tail
    // (`window.pwned2 = 2;`) as a visible paragraph — hidden source leaking into the
    // answer. Whole or split, the sanitizer must still leave no <script> behind.
    const src = 'Intro.\n\n<script>\nwindow.pwned = 1;\n\nwindow.pwned2 = 2;\n</script>\n\nTail.';
    const { container } = render(<MarkdownView streaming>{src}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).not.toContain('window.pwned');
  });

  it('never exposes the tail of a multiline HTML comment as a paragraph (FE2-1)', () => {
    const src =
      'Visible intro.\n\n<!-- hidden reasoning line one\n\nhidden reasoning line two -->\n\nVisible outro.';
    const { container } = render(<MarkdownView streaming>{src}</MarkdownView>, {
      wrapper: MemoryRouter,
    });
    expect(container.textContent).toContain('Visible intro.');
    expect(container.textContent).toContain('Visible outro.');
    expect(container.textContent).not.toContain('hidden reasoning');
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
