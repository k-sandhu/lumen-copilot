/**
 * Bundles the repo's markdown into the STATIC SPA at build time.
 *
 * The SPA has no backend of its own (ADR-0003 / frontend/AGENTS.md), so we cannot
 * "serve" the docs/ folder — instead Vite inlines every doc as a raw string via
 * `import.meta.glob('?raw')`. `docs/` lives a level above the Vite root, so
 * `vite.config.ts` adds the repo root to `server.fs.allow` for the dev server;
 * `vite build` bundles outside-root imports regardless.
 *
 * Keep this file THIN — all logic lives in the pure helpers in ./model.
 */
import { parseDocs, type DocEntry } from './model';

/** Glob keys come back like "../../../../docs/specs/0001.md"; reduce to repo-relative. */
function toRepoRelative(globKey: string): string {
  return globKey.replace(/^(?:\.\.\/)+/, '');
}

function collect(...maps: Record<string, string>[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const map of maps) {
    for (const [key, value] of Object.entries(map)) out[toRepoRelative(key)] = value;
  }
  return out;
}

// Everything under docs/.
const docsRaw = import.meta.glob('../../../../docs/**/*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

// The agent-contract hierarchy + repo READMEs (genuinely "our documentation").
// Display-only here — these files are edited only at their source (AGENTS.md §5).
const contractsRaw = import.meta.glob(
  [
    '../../../../AGENTS.md',
    '../../../../README.md',
    '../../../../backend/AGENTS.md',
    '../../../../backend/README.md',
    '../../../../frontend/AGENTS.md',
    '../../../../frontend/README.md',
    '../../../../contracts/AGENTS.md',
  ],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>;

/** All bundled docs, sorted and grouped-ready. */
export const DOCS: DocEntry[] = parseDocs(collect(docsRaw, contractsRaw));
