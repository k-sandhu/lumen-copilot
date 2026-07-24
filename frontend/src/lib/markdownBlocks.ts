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
 * A link reference definition (`[label]: url`), a footnote definition
 * (`[^label]: …`), or a footnote reference (`[^label]`). These resolve ACROSS
 * blocks, so a per-block split would break them (a reference in one block, its
 * definition in another). When any appear we fall back to a single whole-document
 * parse — correctness over incrementality; such constructs are rare in a chat
 * answer, so the streaming win is preserved for the common case.
 */
const CROSS_BLOCK_REFERENCE = /(^ {0,3}\[[^\]]+\]:\s)|(\[\^)/m;
/** A CommonMark list-item marker (bullet or ordered) with ≤3 leading spaces. */
const LIST_ITEM = /^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)/;
/** An indented (list/quote continuation) line — 1+ leading spaces then content. */
const INDENTED = /^[ \t]+\S/;
/** A fenced code delimiter (``` or ~~~, 3+), with ≤3 leading spaces. */
const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** A closing fence must use the same char and be at least as long as the opener. */
function closesFence(marker: string, opener: string): boolean {
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
 *     OPEN fence keeps everything after it in the trailing block.
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
  let pendingBlanks = 0; // blank lines seen after content, not yet assigned
  let inFence = false;
  let fenceMarker = '';

  const flushSettled = () => {
    if (current.length > 0) {
      settled.push(current.join('\n'));
      current = [];
      currentIsList = false;
    }
  };

  for (const line of lines) {
    if (inFence) {
      current.push(line);
      const close = line.match(FENCE);
      if (close?.[1] !== undefined && closesFence(close[1], fenceMarker)) {
        inFence = false;
        fenceMarker = '';
      }
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
      if (continuesList) {
        // Preserve the blank line(s) so list looseness is unchanged.
        for (let b = 0; b < pendingBlanks; b++) current.push('');
      } else {
        // A blank gap + a fresh block proves the current block is complete.
        flushSettled();
      }
      pendingBlanks = 0;
    }
    if (current.length === 0) currentIsList = LIST_ITEM.test(line);
    current.push(line);
    if (openMarker !== undefined) {
      inFence = true;
      fenceMarker = openMarker;
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
