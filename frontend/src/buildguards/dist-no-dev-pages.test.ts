/**
 * @vitest-environment node
 *
 * Build-artifact guard for the dev-pages gate (issue #40).
 *
 * The security property is NOT "the route is hidden" — it is "the bytes are not
 * shipped". The /docs viewer inlines EVERY repo markdown file (ADRs, specs,
 * `0001-open-decisions.md`, `0004` security invariants, AGENTS.md, …) into its
 * chunk via `import.meta.glob`. A flag-OFF production build must DROP that chunk
 * entirely: static JS assets are fetchable by direct URL regardless of auth or a
 * runtime nav gate, so a runtime-only gate still leaks every internal doc.
 *
 * This test runs a real `vite build` with `VITE_ENABLE_DEV_PAGES` OFF into a temp
 * dir and asserts the emitted bundle contains NONE of:
 *   - internal docs content the docs viewer would inline, and
 *   - the dev-page route/nav markers (so the chunk + its `import()` are gone).
 *
 * It fails if the gate ever regresses to a runtime read (e.g. dynamic
 * `import.meta.env[key]`), which Vite cannot inline and Rollup cannot tree-shake
 * — exactly the bug this test was added to lock out. A companion ON build proves
 * the same content IS present when the flag is enabled, so the assertion can't
 * pass by accident (e.g. a renamed string).
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { build } from 'vite';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const frontendRoot = fileURLToPath(new URL('../../', import.meta.url));

// Strings that only exist in the dev-page chunks (inlined docs) or in the
// dev-page route/nav wiring. If ANY appears in a flag-OFF build, internal
// material leaked into a directly-fetchable static asset.
const FORBIDDEN_IN_OFF_BUILD = [
  // --- inlined internal docs (docs viewer `import.meta.glob`) ---
  'permissioned by default', // AGENTS.md §2 decision filter
  'open-decisions', // docs/specs/0001-open-decisions.md (filename + links)
  'Definition of Ready', // WORK_TRACKING / process docs
  'Definition of Done', // AGENTS.md §15
  'Architecture Decision Record', // ADR titles
  'security invariants', // spec 0004 / AGENTS.md §4
  // --- dev-page route + nav markers (chunk must be gone, not just hidden) ---
  'DocsPage',
  'FeaturesPage',
  'Features built',
] as const;

/** Run a production build into a fresh temp outDir and return all emitted text. */
async function buildAndCollect(env: Record<string, string>): Promise<string> {
  const outDir = mkdtempSync(join(tmpdir(), 'lumen-distcheck-'));
  try {
    // Apply the flag via process.env so vite.config's loadEnv/process.env read
    // picks it up; build in 'production' mode (never 'test'), exactly like a real
    // `pnpm build`.
    const prev: Record<string, string | undefined> = {};
    for (const [k, v] of Object.entries(env)) {
      prev[k] = process.env[k];
      process.env[k] = v;
    }
    try {
      await build({
        root: frontendRoot,
        mode: 'production',
        logLevel: 'silent',
        build: { outDir, emptyOutDir: true, chunkSizeWarningLimit: 5000 },
      });
    } finally {
      for (const [k, v] of Object.entries(prev)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
    }
    return readAllAssets(outDir);
  } finally {
    rmSync(outDir, { recursive: true, force: true });
  }
}

/** Concatenate every emitted JS/CSS/HTML asset into one searchable string. */
function readAllAssets(outDir: string): string {
  const assetsDir = join(outDir, 'assets');
  const files = readdirSync(assetsDir).filter((f) => /\.(js|css)$/.test(f));
  const html = readFileSync(join(outDir, 'index.html'), 'utf8');
  return [html, ...files.map((f) => readFileSync(join(assetsDir, f), 'utf8'))].join('\n');
}

describe('dev-pages build-time elimination (issue #40)', () => {
  // A real vite build is slow; run each once and share the result.
  let offBundle = '';
  let onBundle = '';

  beforeAll(async () => {
    offBundle = await buildAndCollect({ VITE_ENABLE_DEV_PAGES: 'false' });
    onBundle = await buildAndCollect({ VITE_ENABLE_DEV_PAGES: 'true' });
  }, 180_000);

  afterAll(() => {
    offBundle = '';
    onBundle = '';
  });

  it.each(FORBIDDEN_IN_OFF_BUILD)('a flag-OFF production build does NOT emit %j', (needle) => {
    expect(offBundle).not.toContain(needle);
  });

  it('a flag-OFF build sanity check: it actually produced assets', () => {
    // Guards against a false pass where the build emitted nothing and every
    // `not.toContain` trivially holds.
    expect(offBundle.length).toBeGreaterThan(10_000);
    expect(offBundle).toContain('Lumen'); // the real app still ships
  });

  it('a flag-ON build DOES emit the dev-page content (proves the needles are real)', () => {
    // If these strings never appeared in any build, the OFF assertions would be
    // vacuous. The ON build must contain the inlined docs + dev-page markers.
    expect(onBundle).toContain('permissioned by default');
    expect(onBundle).toContain('open-decisions');
    expect(onBundle).toContain('Features built');
  });
});
