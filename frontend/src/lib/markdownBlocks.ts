/**
 * Streaming markdown block splitter (issue #494).
 *
 * Splits a (possibly partial) markdown source into SETTLED blocks — each a
 * complete, self-contained top-level block that can be parsed once and memoised —
 * plus one TRAILING in-progress block that re-parses as deltas land. Kept in its
 * own module (not `markdown.tsx`) so the renderer file stays a pure-component
 * module and this pure logic can be unit-tested directly.
 */

/**
 * Any construct that can resolve ACROSS block boundaries, so a per-block split
 * would render it differently from a whole-document parse. Three shapes:
 *   1. a link-reference / footnote DEFINITION (`[label]: url`, `[^label]: …`),
 *   2. a footnote REFERENCE (`[^label]`),
 *   3. a reference-style link USE — full `[text][id]`, collapsed `[text][]`, or
 *      SHORTCUT `[label]` (syntactically identical to a citation marker) — i.e. any
 *      `[…]` NOT immediately followed by `(` (the `(?!\()` excludes self-contained
 *      inline links/images `[text](url)` / `![alt](url)`, whose target is inline).
 * Why the USE (shape 3) matters, not just the definition: a shortcut/reference use
 * can appear and let earlier blocks SETTLE, and then the matching DEFINITION lands
 * in a LATER delta — which would (a) un-settle & remount every earlier block and
 * (b) retroactively turn the use into a link, so the settled standalone render (a
 * literal `[label]`) would no longer equal the one-shot render (a link). Tripping
 * on the USE keeps the whole source in the trailing region from the moment the use
 * appears — BEFORE anything settles — so the settled set stays append-only and the
 * final DOM still matches one-shot. Correctness over granularity: err toward
 * trailing. (Cost: an answer carrying `[n]` citation markers streams as one
 * re-parsed block rather than settling incrementally — acceptable, and rare vs. the
 * bracket-free common case.)
 */
const CROSS_BLOCK_REFERENCE = /(^ {0,3}\[[^\]]+\]:\s)|(\[\^)|(\[[^\]]*\](?!\())/m;
/**
 * CommonMark HTML blocks whose END condition is a TERMINATOR STRING rather than a
 * blank line — start conditions 1–5 (§4.6). Such a block legally CONTAINS blank
 * lines, so the blank-line boundary below must not settle inside one: a one-shot
 * parse keeps `<!-- a\n\nb -->` whole (the pipeline drops raw HTML, so nothing of it
 * renders), while a split settles `<!-- a` and then renders `b -->` as a visible
 * paragraph — a DOM divergence AND a leak of hidden source text into the answer.
 * Note the end condition may already be satisfied on the START line
 * (`<!-- one line -->`), in which case the block never spans lines at all.
 * Conditions 6 (`<div>`, `<table>`, … ) and 7 (a bare complete tag) END at a blank
 * line, so the existing boundary already matches one-shot for them — they are
 * deliberately absent here.
 */
const HTML_BLOCK: ReadonlyArray<{ start: RegExp; end: RegExp }> = [
  // 1. <script | <pre | <style | <textarea  →  the matching close tag.
  {
    start: /^ {0,3}<(?:script|pre|style|textarea)(?:[ \t>]|$)/i,
    end: /<\/(?:script|pre|style|textarea)>/i,
  },
  // 2. comment, 3. processing instruction, 4. declaration, 5. CDATA.
  { start: /^ {0,3}<!--/, end: /-->/ },
  { start: /^ {0,3}<\?/, end: /\?>/ },
  { start: /^ {0,3}<![A-Za-z]/, end: />/ },
  { start: /^ {0,3}<!\[CDATA\[/, end: /\]\]>/ },
];
/** A CommonMark list-item marker (bullet or ordered) with ≤3 leading spaces. */
const LIST_ITEM = /^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)/;
/** An indented (list/quote continuation) line — 1+ leading spaces then content. */
const INDENTED = /^[ \t]+\S/;
/**
 * An indented **code-block** line (CommonMark: ≥4 leading spaces, or a tab). Unlike
 * a lazy continuation, an indented code block absorbs its INTERNAL blank lines — a
 * blank between two indented lines is part of the code, not a block separator — so
 * it must not be split on those blanks (would render as two <pre>s ≠ one-shot).
 */
const INDENTED_CODE = /^(?: {4,}|\t)/;
/** A fenced code OPENER: ``` or ~~~ (3+), ≤3 leading spaces (an info string may follow). */
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
/**
 * A closing fence (CommonMark §119): ≤3 leading spaces, a run of the fence char,
 * then ONLY spaces/tabs to end of line. Unlike the opener it may carry NO info
 * string — so `\`\`\`not-a-close` is content INSIDE the block, never a close.
 */
const CLOSING_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;

/**
 * Does `line` close a fence opened by `opener`? It must be a bare closing fence
 * (whitespace-only after the run — see CLOSING_FENCE), use the SAME char, and be at
 * least as long as the opener. Matching the opener FENCE regex is not enough: that
 * accepts any fence-prefixed line, so `\`\`\`not-a-close` would wrongly settle the
 * block and the following content would render as extra paragraphs / a second fence.
 */
function closesFence(line: string, opener: string): boolean {
  const marker = line.match(CLOSING_FENCE)?.[1];
  if (marker === undefined) return false;
  return marker[0] === opener[0] && marker.length >= opener.length;
}

export interface StreamingBlocks {
  /** Complete, self-contained top-level blocks (append-only as text arrives). */
  settled: string[];
  /** The in-progress final block, re-parsed as deltas land ('' when none). */
  trailing: string;
}

/**
 * Split a (possibly partial) markdown source into settled blocks + one trailing
 * in-progress block (#494). Invariants that keep the settled render identical to a
 * one-shot render (AC-3):
 *   - A block is settled ONLY when a following blank line AND a non-continuation
 *     block prove it cannot still be extended. The final region is always trailing.
 *   - Fenced code blocks are never split on their internal blank lines, and an
 *     OPEN fence keeps everything after it in the trailing block. The same holds
 *     for an HTML block whose end condition is a terminator string (HTML_BLOCK).
 *   - A list is not split across its blank lines (a loose list must stay one
 *     <ul>/<ol>); indented continuations stay attached to their list item.
 *   - When in doubt, MERGE into the trailing region — over-merging renders the
 *     same as one-shot; only a premature SPLIT can corrupt the DOM.
 */
export function splitStreamingBlocks(source: string): StreamingBlocks {
  if (source === '') return { settled: [], trailing: '' };
  if (CROSS_BLOCK_REFERENCE.test(source)) return { settled: [], trailing: source };

  const lines = source.split('\n');
  const settled: string[] = [];
  let current: string[] = []; // lines of the in-progress block
  let currentIsList = false; // the current block is (or extends) a list
  let currentIsIndentedCode = false; // the current block is an indented code block
  let pendingBlanks = 0; // blank lines seen after content, not yet assigned
  let inFence = false;
  let fenceMarker = '';
  let htmlEnd: RegExp | null = null; // terminator of the open HTML block, if any

  const flushSettled = () => {
    if (current.length > 0) {
      settled.push(current.join('\n'));
      current = [];
      currentIsList = false;
      currentIsIndentedCode = false;
    }
  };

  for (const line of lines) {
    if (inFence) {
      current.push(line);
      if (closesFence(line, fenceMarker)) {
        inFence = false;
        fenceMarker = '';
      }
      continue;
    }
    // An open HTML block (conditions 1–5) runs to its terminator — blank lines are
    // CONTENT, not a block boundary, so they must never settle it.
    if (htmlEnd !== null) {
      current.push(line);
      if (htmlEnd.test(line)) htmlEnd = null;
      continue;
    }

    if (line.trim() === '') {
      if (current.length > 0) pendingBlanks++;
      continue; // leading blanks are dropped
    }

    // A non-blank content line (possibly a fence opener).
    const openMarker = line.match(FENCE)?.[1];
    if (pendingBlanks > 0) {
      const continuesList = currentIsList && (LIST_ITEM.test(line) || INDENTED.test(line));
      // An indented code block keeps going across a blank line iff a later
      // indented-code line follows; the blank is code content, not a separator.
      const continuesIndentedCode = currentIsIndentedCode && INDENTED_CODE.test(line);
      if (continuesList || continuesIndentedCode) {
        // Preserve the blank line(s) so list looseness / code content is unchanged.
        for (let b = 0; b < pendingBlanks; b++) current.push('');
      } else {
        // A blank gap + a fresh block proves the current block is complete.
        flushSettled();
      }
      pendingBlanks = 0;
    }
    if (current.length === 0) {
      currentIsList = LIST_ITEM.test(line);
      // An indented code block only starts outside a list (a list marker with
      // deep indent is still a list item, handled above).
      currentIsIndentedCode = !currentIsList && INDENTED_CODE.test(line);
    }
    current.push(line);
    if (openMarker !== undefined) {
      inFence = true;
      fenceMarker = openMarker;
    } else {
      // A multiline-capable HTML block may open on ANY content line (conditions
      // 1–6 can even interrupt a paragraph), so this is checked per line, not only
      // at a block start.
      const html = HTML_BLOCK.find((h) => h.start.test(line));
      if (html !== undefined && !html.end.test(line)) htmlEnd = html.end;
    }
  }

  // The final region is always in-progress: more text could still extend it.
  return { settled, trailing: current.join('\n') };
}

/** Small, stable content hash (djb2) for a settled block's React key (#494). */
export function blockKey(block: string): string {
  let h = 5381;
  for (let i = 0; i < block.length; i++) h = (Math.imul(h, 33) + block.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}
