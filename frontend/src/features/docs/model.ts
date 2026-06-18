/**
 * Docs model — PURE, dependency-free transforms over the raw markdown map that
 * `content.ts` produces from `import.meta.glob`. Kept pure so the slug/group/title
 * derivation and the in-app link resolver are unit-testable without the bundler
 * (see model.test.ts / content.test.ts).
 *
 * A "doc" is any markdown file we bundle: everything under repo `docs/`, plus the
 * top-level agent contracts (AGENTS.md hierarchy) and repo READMEs. Each becomes a
 * routable entry under `/docs/<slug>`.
 */

export interface DocEntry {
  /** Route segment after `/docs/`, no extension. e.g. "architecture/0003-application-stack". */
  slug: string;
  /** First `# ` heading, else the filename. */
  title: string;
  /** Grouping key for the nav (see GROUP_ORDER). */
  group: string;
  /** Repo-relative path WITH extension. e.g. "docs/architecture/0003-application-stack.md". */
  path: string;
  /** Raw markdown source. */
  content: string;
}

export interface DocGroup {
  key: string;
  label: string;
  entries: DocEntry[];
}

/** Display order for known groups; anything else sorts after, alphabetically. */
export const GROUP_ORDER: readonly string[] = [
  'Contracts',
  'Repo',
  'architecture',
  'specs',
  'product',
  'roles',
  'General',
];

const GROUP_LABELS: Record<string, string> = {
  Contracts: 'Agent contracts',
  Repo: 'Repo readmes',
  architecture: 'Architecture (ADRs)',
  specs: 'Specs',
  product: 'Product',
  roles: 'Roles',
  General: 'General',
};

export function groupLabel(key: string): string {
  return GROUP_LABELS[key] ?? key;
}

function stripMd(p: string): string {
  return p.replace(/\.md$/i, '');
}

function basename(p: string): string {
  return p.split('/').pop() ?? p;
}

function deriveTitle(content: string, slug: string): string {
  const m = content.match(/^\s{0,3}#\s+(.+?)\s*$/m);
  if (m && m[1]) return m[1].trim();
  return basename(slug);
}

/** Map one repo-relative markdown path + content to a DocEntry. */
function toEntry(path: string, content: string): DocEntry {
  let slug: string;
  let group: string;
  if (path.startsWith('docs/')) {
    const rel = path.slice('docs/'.length); // e.g. "architecture/0003-x.md" or "WORK_TRACKING.md"
    slug = stripMd(rel);
    const slash = rel.indexOf('/');
    group = slash === -1 ? 'General' : rel.slice(0, slash);
  } else {
    // Top-level contracts / readmes (AGENTS.md hierarchy, README.md, …).
    slug = stripMd(path);
    group = basename(path).toLowerCase() === 'agents.md' ? 'Contracts' : 'Repo';
  }
  return { slug, title: deriveTitle(content, slug), group, path, content };
}

function groupRank(g: string): number {
  const i = GROUP_ORDER.indexOf(g);
  return i === -1 ? GROUP_ORDER.length : i;
}

/** Within a group, READMEs float to the top, then alphabetical by slug. */
function compareWithinGroup(a: DocEntry, b: DocEntry): number {
  const ra = basename(a.slug).toLowerCase() === 'readme' ? 0 : 1;
  const rb = basename(b.slug).toLowerCase() === 'readme' ? 0 : 1;
  if (ra !== rb) return ra - rb;
  return a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0;
}

function sortEntries(entries: DocEntry[]): DocEntry[] {
  return [...entries].sort((a, b) => {
    const gr = groupRank(a.group) - groupRank(b.group);
    if (gr !== 0) return gr;
    if (a.group !== b.group) return a.group < b.group ? -1 : 1;
    return compareWithinGroup(a, b);
  });
}

/**
 * Parse the raw `{ repoRelativePath: markdown }` map into sorted DocEntries.
 * Pure — fed the glob output by content.ts, or a synthetic map by tests.
 */
export function parseDocs(raw: Record<string, string>): DocEntry[] {
  const entries = Object.entries(raw).map(([path, content]) => toEntry(path, content));
  return sortEntries(entries);
}

/** Group sorted entries for the nav, preserving GROUP_ORDER then extras. */
export function groupDocs(entries: DocEntry[]): DocGroup[] {
  const byGroup = new Map<string, DocEntry[]>();
  for (const e of entries) {
    const arr = byGroup.get(e.group);
    if (arr) arr.push(e);
    else byGroup.set(e.group, [e]);
  }
  const groups: DocGroup[] = [];
  const seen = new Set<string>();
  for (const key of GROUP_ORDER) {
    const arr = byGroup.get(key);
    if (arr) {
      groups.push({ key, label: groupLabel(key), entries: arr });
      seen.add(key);
    }
  }
  for (const [key, arr] of byGroup) {
    if (!seen.has(key)) groups.push({ key, label: groupLabel(key), entries: arr });
  }
  return groups;
}

/** Reverse index: repo-relative path → slug, for link resolution. */
export function buildPathIndex(entries: DocEntry[]): Record<string, string> {
  const index: Record<string, string> = {};
  for (const e of entries) index[e.path] = e.slug;
  return index;
}

function dirOf(p: string): string {
  const i = p.lastIndexOf('/');
  return i === -1 ? '' : p.slice(0, i);
}

/** POSIX-style path normalize that collapses `.`/`..` and empty segments. */
function normalizePath(p: string): string {
  const out: string[] = [];
  for (const part of p.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') {
      out.pop();
      continue;
    }
    out.push(part);
  }
  return out.join('/');
}

/**
 * Resolve a markdown link found inside `currentPath` to an in-app docs route, or
 * null when it's external / unknown (caller renders a normal new-tab <a>). This is
 * what lets doc-to-doc links (e.g. AGENTS.md → docs/specs/0003-…md, or an ADR's
 * ../specs/0001-…md) navigate inside the viewer instead of opening broken tabs.
 */
export function resolveDocLink(
  currentPath: string,
  href: string | undefined,
  pathIndex: Record<string, string>,
): string | null {
  if (!href) return null;
  if (/^(https?:)?\/\//i.test(href)) return null; // absolute or protocol-relative URL
  if (/^(mailto:|tel:|data:)/i.test(href)) return null;
  const clean = href.split('#')[0]?.split('?')[0] ?? '';
  if (clean === '') return null; // pure in-page anchor / query
  const base = dirOf(currentPath);
  const target = normalizePath(base ? `${base}/${clean}` : clean);
  const slug = pathIndex[target];
  return slug !== undefined ? `/docs/${slug}` : null;
}
