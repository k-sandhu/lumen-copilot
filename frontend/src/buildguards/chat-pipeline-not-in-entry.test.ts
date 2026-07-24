/**
 * @vitest-environment node
 *
 * Bundle guard for issue #494 (AC-5): the markdown + highlight.js pipeline must be
 * OUT of the entry chunk.
 *
 * Chat was historically the ONE non-lazy feature route (`features/chat/route.tsx`
 * imported `App` statically), so `App → ChatView → MessageBubble → lib/markdown`
 * dragged react-markdown + the unified/remark/rehype/micromark ecosystem +
 * highlight.js into the entry chunk — loaded on first paint regardless of landing
 * route. #494 does two things: makes the chat route lazy (like every other
 * feature) and adds a `manualChunks` rule (vite.config.ts) that pins the whole
 * markdown/highlight pipeline into its own `markdown` chunk.
 *
 * This runs a real production `vite build` (write:false, no temp dir) and walks the
 * Rollup chunk graph. It asserts the pipeline modules are NOT in the entry chunk
 * and DO live in a dedicated, separate chunk (so the assertion can't pass by the
 * build simply dropping them).
 *
 * KNOWN RESIDUAL (reported as a follow-up, NOT in #494's scope): a pre-existing
 * chat↔preferences barrel cycle — `ChatView` imports `@/features/preferences`
 * while `preferences/DefaultModelSetting` imports the heavy `@/features/chat`
 * barrel for the shared `useModels` registry — keeps app-level chat consumers in
 * the entry chunk, so the `markdown` chunk is still STATICALLY imported by the
 * entry (a separate cacheable file, but not yet deferred off first paint). Fully
 * deferring the download needs that cycle broken (relocating `useModels` out of
 * the chat barrel — it is also used by the assistants slice), which is outside
 * this issue. This guard therefore pins the achievable, stable invariant: the
 * pipeline is out of the entry chunk itself.
 *
 * The existing `dist-no-dev-pages.test.ts` collector joins every asset into one
 * flat string and cannot express "which chunk", so this needs its own build. It
 * costs ~one production build (~10s); it lives in `beforeAll(…, 180_000)`.
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { build, type Rollup } from 'vite';
import { fileURLToPath } from 'node:url';

const frontendRoot = fileURLToPath(new URL('../../', import.meta.url));

// Substrings that identify the heavy markdown/highlight pipeline in a module id.
// With pnpm the real path is `…/node_modules/.pnpm/<pkg>@<ver>/node_modules/<pkg>/…`,
// so the final `node_modules/<pkg>` segment still matches.
const PIPELINE_MARKERS = [
  'node_modules/react-markdown',
  'node_modules/highlight.js',
  'node_modules/rehype-highlight',
  'node_modules/rehype-sanitize',
  'node_modules/remark-gfm',
  'node_modules/micromark',
] as const;

/** Module ids are OS-native paths; compare on posix separators. */
function moduleIdsOf(chunk: Rollup.OutputChunk): string[] {
  return Object.keys(chunk.modules).map((id) => id.replace(/\\/g, '/'));
}

function pipelineMarkersIn(chunk: Rollup.OutputChunk): string[] {
  const ids = moduleIdsOf(chunk);
  return PIPELINE_MARKERS.filter((m) => ids.some((id) => id.includes(m)));
}

describe('markdown/highlight pipeline is out of the entry chunk (#494 AC-5)', () => {
  let chunks: Rollup.OutputChunk[] = [];
  let entry: Rollup.OutputChunk | undefined;

  beforeAll(async () => {
    const prev = process.env.VITE_ENABLE_DEV_PAGES;
    process.env.VITE_ENABLE_DEV_PAGES = 'false';
    try {
      const result = await build({
        root: frontendRoot,
        mode: 'production',
        logLevel: 'silent',
        build: { write: false, chunkSizeWarningLimit: 5000 },
      });
      const outputs = Array.isArray(result) ? result : [result];
      chunks = outputs
        .flatMap((o) => ('output' in o ? o.output : []))
        .filter((o): o is Rollup.OutputChunk => o.type === 'chunk');
      entry = chunks.find((c) => c.isEntry);
    } finally {
      if (prev === undefined) delete process.env.VITE_ENABLE_DEV_PAGES;
      else process.env.VITE_ENABLE_DEV_PAGES = prev;
    }
  }, 180_000);

  afterAll(() => {
    chunks = [];
    entry = undefined;
  });

  it('produced exactly one entry chunk and code-split into several chunks', () => {
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.filter((c) => c.isEntry)).toHaveLength(1);
    expect(entry).toBeDefined();
  });

  it('the entry chunk contains NONE of the markdown/highlight pipeline modules', () => {
    // The exact regression #494 fixes: highlight.js + react-markdown + micromark
    // were in the entry chunk before. They must not be now.
    expect(entry ? pipelineMarkersIn(entry) : ['<no entry>']).toEqual([]);
  });

  it('the entry chunk has no highlight.js runtime signature inline', () => {
    // Belt-and-suspenders: the pre-#494 entry chunk contained `highlightAuto`.
    expect(entry?.code ?? '').not.toContain('highlightAuto');
  });

  it('the pipeline IS emitted, gathered into a dedicated non-entry chunk (non-vacuity)', () => {
    const pipelineChunks = chunks.filter((c) => pipelineMarkersIn(c).length > 0);
    expect(pipelineChunks.length).toBeGreaterThan(0);
    // Every chunk that holds pipeline modules is a lazy (non-entry) chunk…
    for (const c of pipelineChunks) expect(c.isEntry).toBe(false);
    // …and the heavy pipeline is consolidated (the manualChunks `markdown` group),
    // not scattered across many first-paint chunks.
    const named = pipelineChunks.find((c) => c.name === 'markdown');
    expect(named).toBeDefined();
  });
});
