/**
 * @vitest-environment node
 *
 * Bundle guard for issue #494 (AC-5): the markdown + highlight.js pipeline must
 * not be downloaded or evaluated on first paint.
 *
 * Chat was historically the ONE non-lazy feature route (`features/chat/route.tsx`
 * imported `App` statically), so `App → ChatView → MessageBubble → lib/markdown`
 * dragged react-markdown + the unified/remark/rehype/micromark ecosystem +
 * highlight.js into the entry chunk — loaded on first paint regardless of landing
 * route. #494 does two things: makes the chat route lazy (like every other
 * feature) and adds a `manualChunks` rule (vite.config.ts) that pins the whole
 * markdown/highlight pipeline into its own `markdown` chunk.
 *
 * WHY THE OWNERSHIP CHECK ALONE IS NOT ENOUGH (the FE-9 finding). "Which chunk
 * owns the pipeline modules" says nothing about whether the browser fetches that
 * chunk on first paint. A chunk-to-chunk STATIC import is a first-paint download:
 * the entry's `import "./markdown-*.js"` is resolved and evaluated before the app
 * renders. The repo used to be in exactly that state through a chat↔preferences
 * barrel cycle —
 *
 *   AppShell → AppearanceMenu → preferences/DefaultModelSetting
 *            → `@/features/chat` (barrel) → ChatView → lib/markdown
 *   ChatView → `@/features/preferences` (barrel) → DefaultModelSetting  ← cycle
 *
 * — which Rollup reported as CIRCULAR_DEPENDENCY + CYCLIC_CROSS_CHUNK_REEXPORT
 * and which left the entry chunk statically importing the `markdown` chunk. The
 * cycle is now broken: the shared model registry lives in its own tiny
 * `features/models` slice, so neither barrel reaches the other.
 *
 * This runs a real production `vite build` (write:false, no temp dir) and asserts
 * the property that actually matters:
 *   1. the pipeline modules are not in the entry chunk, AND
 *   2. no chunk in the entry's TRANSITIVE STATIC import closure holds them, AND
 *   3. they are still emitted, in a dedicated non-entry `markdown` chunk that is
 *      reachable only dynamically (non-vacuity — the assertion cannot pass by the
 *      build simply dropping them), AND
 *   4. the build reports no app-source circular-dependency warning — the symptom
 *      that produced (2) in the first place, and the thing a future barrel import
 *      would reintroduce.
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

/** Rollup warning codes that flag a module cycle. */
const CYCLE_WARNING_CODES = ['CIRCULAR_DEPENDENCY', 'CYCLIC_CROSS_CHUNK_REEXPORT'];

/** Module ids are OS-native paths; compare on posix separators. */
function moduleIdsOf(chunk: Rollup.OutputChunk): string[] {
  return Object.keys(chunk.modules).map((id) => id.replace(/\\/g, '/'));
}

function pipelineMarkersIn(chunk: Rollup.OutputChunk): string[] {
  const ids = moduleIdsOf(chunk);
  return PIPELINE_MARKERS.filter((m) => ids.some((id) => id.includes(m)));
}

describe('markdown/highlight pipeline is off the first-paint path (#494 AC-5)', () => {
  let chunks: Rollup.OutputChunk[] = [];
  let byFile = new Map<string, Rollup.OutputChunk>();
  let entry: Rollup.OutputChunk | undefined;
  let warnings: { code: string; message: string }[] = [];

  /**
   * Every chunk the browser must fetch+evaluate before the app renders: the
   * entry plus everything it reaches through STATIC imports (`chunk.imports`).
   * `chunk.dynamicImports` are deliberately excluded — those are the deferred
   * `import()` boundaries #494 introduced.
   */
  function entryStaticClosure(): Rollup.OutputChunk[] {
    const seen = new Set<string>();
    const stack = entry ? [entry.fileName] : [];
    while (stack.length > 0) {
      const fileName = stack.pop() as string;
      if (seen.has(fileName)) continue;
      seen.add(fileName);
      for (const imported of byFile.get(fileName)?.imports ?? []) stack.push(imported);
    }
    return [...seen].flatMap((f) => byFile.get(f) ?? []);
  }

  beforeAll(async () => {
    const prev = process.env.VITE_ENABLE_DEV_PAGES;
    process.env.VITE_ENABLE_DEV_PAGES = 'false';
    try {
      const collected: { code: string; message: string }[] = [];
      const result = await build({
        root: frontendRoot,
        mode: 'production',
        logLevel: 'silent',
        build: {
          write: false,
          chunkSizeWarningLimit: 5000,
          rollupOptions: {
            // Capture warnings instead of printing them; assertion 4 reads these.
            onwarn(warning) {
              collected.push({ code: warning.code ?? '', message: warning.message });
            },
          },
        },
      });
      warnings = collected;
      const outputs = Array.isArray(result) ? result : [result];
      chunks = outputs
        .flatMap((o) => ('output' in o ? o.output : []))
        .filter((o): o is Rollup.OutputChunk => o.type === 'chunk');
      byFile = new Map(chunks.map((c) => [c.fileName, c]));
      entry = chunks.find((c) => c.isEntry);
    } finally {
      if (prev === undefined) delete process.env.VITE_ENABLE_DEV_PAGES;
      else process.env.VITE_ENABLE_DEV_PAGES = prev;
    }
  }, 180_000);

  afterAll(() => {
    chunks = [];
    byFile = new Map();
    entry = undefined;
    warnings = [];
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

  it('NO chunk in the entry’s transitive STATIC import closure holds the pipeline (FE-9)', () => {
    // The property the ownership check could not express: a separate `markdown`
    // chunk is still a first-paint download if the entry statically imports it
    // (directly or through any chunk it statically imports). Owning the modules
    // elsewhere only defers the cost when nothing on the static path pulls it in.
    expect(entry).toBeDefined();
    const offenders = entryStaticClosure()
      .filter((c) => pipelineMarkersIn(c).length > 0)
      .map((c) => `${c.fileName} (name=${c.name}) → ${pipelineMarkersIn(c).join(', ')}`);
    expect(offenders).toEqual([]);
  });

  it('the pipeline IS emitted in a dedicated non-entry chunk, reached only dynamically', () => {
    const pipelineChunks = chunks.filter((c) => pipelineMarkersIn(c).length > 0);
    expect(pipelineChunks.length).toBeGreaterThan(0);
    // Every chunk that holds pipeline modules is a lazy (non-entry) chunk…
    for (const c of pipelineChunks) expect(c.isEntry).toBe(false);
    // …and the heavy pipeline is consolidated (the manualChunks `markdown` group),
    // not scattered across many first-paint chunks.
    const named = pipelineChunks.find((c) => c.name === 'markdown');
    expect(named).toBeDefined();
    // …and something in the graph DOES import it, so it still ships to the users
    // who navigate to a content screen (the deferral is real, not a deletion).
    const importers = chunks.filter(
      (c) => named !== undefined && [...c.imports, ...c.dynamicImports].includes(named.fileName),
    );
    expect(importers.length).toBeGreaterThan(0);
  });

  it('the build reports no circular dependency between app source modules (FE-9)', () => {
    // The chat↔preferences barrel cycle surfaced here first. Rollup's
    // CYCLIC_CROSS_CHUNK_REEXPORT is explicit that such a cycle "will produce a
    // circular dependency between chunks and will likely lead to broken execution
    // order" — and it is what glued the `markdown` chunk onto the entry. Cycles
    // inside third-party packages are out of our control and not asserted.
    const appCycles = warnings
      .filter((w) => CYCLE_WARNING_CODES.includes(w.code))
      .filter((w) => w.message.includes('src/features/') || w.message.includes('src/routes/'))
      .map((w) => `${w.code}: ${w.message}`);
    expect(appCycles).toEqual([]);
  });
});
