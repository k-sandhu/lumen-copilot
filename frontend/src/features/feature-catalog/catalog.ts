/**
 * The "features built" catalog — a CURATED, GROUNDED list of what has actually
 * shipped (the SPA is static, so we can't query GitHub at runtime). Every entry
 * traces to a merged PR and/or an ADR/spec; internal links point into the in-app
 * docs viewer (/docs/<slug>), external links to GitHub.
 *
 * MAINTENANCE RULE: when a feature lands, add/flip its entry here in the SAME PR
 * (AGENTS.md §7.6 "living docs update in the same change"). The integrity test in
 * catalog.test.ts fails if any /docs/ link here stops resolving to a real doc.
 */

export type FeatureStatus = 'shipped' | 'in-progress' | 'planned';

export interface FeatureLink {
  label: string;
  /** In-app route ("/docs/…", "/features") or an external URL ("https://…"). */
  href: string;
}

export interface CatalogEntry {
  id: string;
  title: string;
  area: string;
  status: FeatureStatus;
  summary: string;
  links: FeatureLink[];
}

export interface AreaGroup {
  area: string;
  entries: CatalogEntry[];
}

const GH = 'https://github.com/k-sandhu/lumen-copilot';

/** Display order for areas; unlisted areas sort after, alphabetically. */
export const AREA_ORDER: readonly string[] = [
  'Platform',
  'Backend',
  'Storage',
  'LLM',
  'Contracts',
  'Frontend',
  'Retrieval',
  'Security',
];

export const CATALOG: readonly CatalogEntry[] = [
  {
    id: 'local-stack',
    title: 'One-command local stack',
    area: 'Platform',
    status: 'shipped',
    summary:
      '`docker compose up` brings up FastAPI, Postgres + pgvector, Redis, MinIO, Celery and the SPA on non-colliding host ports.',
    links: [
      { label: 'ADR-0005', href: '/docs/architecture/0005-local-run-and-developer-workflow' },
      { label: 'PR #30', href: `${GH}/pull/30` },
    ],
  },
  {
    id: 'boundaries',
    title: 'Architecture boundaries & adapters',
    area: 'Platform',
    status: 'shipped',
    summary:
      'One module owns each external system; adapters expose domain types (never vendor types), with one-directional api/ → services/ → domain/ layering.',
    links: [
      { label: 'ADR-0004', href: '/docs/architecture/0004-architecture-boundaries-and-adapters' },
    ],
  },
  {
    id: 'backend-skeleton',
    title: 'FastAPI backend service',
    area: 'Backend',
    status: 'shipped',
    summary: 'Async FastAPI app (Python 3.12) with the layered structure and the contract wiring.',
    links: [
      { label: 'ADR-0003', href: '/docs/architecture/0003-application-stack' },
      { label: 'PR #30', href: `${GH}/pull/30` },
    ],
  },
  {
    id: 'health-readiness',
    title: 'Health & readiness probes',
    area: 'Backend',
    status: 'shipped',
    summary:
      '`/health` and `/health/ready` report liveness and per-dependency readiness (Postgres / Redis / MinIO).',
    links: [{ label: 'PR #30', href: `${GH}/pull/30` }],
  },
  {
    id: 'ws-health',
    title: 'Realtime WebSocket health stream',
    area: 'Backend',
    status: 'shipped',
    summary:
      '`/ws/health` streams typed envelopes over WebSocket, on the Redis pub/sub backplane that chat streaming will reuse.',
    links: [
      { label: 'ADR-0003', href: '/docs/architecture/0003-application-stack' },
      { label: 'PR #30', href: `${GH}/pull/30` },
    ],
  },
  {
    id: 'storage-sandbox',
    title: 'Object-storage upload sandbox',
    area: 'Storage',
    status: 'shipped',
    summary: 'MinIO-backed uploads, tenant-prefixed and content-addressed (CC-12).',
    links: [
      { label: 'ADR-0004', href: '/docs/architecture/0004-architecture-boundaries-and-adapters' },
      { label: 'PR #31', href: `${GH}/pull/31` },
    ],
  },
  {
    id: 'llm-gateway',
    title: 'LLM model gateway',
    area: 'LLM',
    status: 'shipped',
    summary:
      'LiteLLM → OpenRouter gateway: the single chokepoint for model calls, with typed errors and clean degradation (CC-9).',
    links: [
      { label: 'ADR-0003', href: '/docs/architecture/0003-application-stack' },
      { label: 'PR #33', href: `${GH}/pull/33` },
    ],
  },
  {
    id: 'wire-contract',
    title: 'FE/BE wire contract',
    area: 'Contracts',
    status: 'shipped',
    summary:
      'Frozen OpenAPI + WebSocket envelope schemas — the single source of truth the generated client builds from.',
    links: [
      { label: 'ADR-0006', href: '/docs/architecture/0006-contract-first-parallel-implementation' },
      { label: 'contracts/AGENTS', href: '/docs/contracts/AGENTS' },
    ],
  },
  {
    id: 'frontend-shell',
    title: 'Frontend app shell',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'React + Vite SPA: light/dark theming, independent-scroll two-pane layout, and the sanitized markdown pipeline.',
    links: [
      { label: 'ADR-0003', href: '/docs/architecture/0003-application-stack' },
      { label: 'PR #30', href: `${GH}/pull/30` },
    ],
  },
  {
    id: 'system-status',
    title: 'System status panel',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'Live backend readiness + WebSocket indicator with full loading / empty / error / success states.',
    links: [{ label: 'PR #30', href: `${GH}/pull/30` }],
  },
  {
    id: 'frontend-auth',
    title: 'Frontend auth — login, session & bearer wiring',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'Login screen against POST /auth/login, in-memory access-token storage, every request bearer-authed, silent refresh-then-retry on 401, GET /auth/me, logout, and a route guard (unauthenticated → login, authenticated → app shell).',
    links: [
      {
        label: 'Security invariants',
        href: '/docs/specs/0004-security-and-domain-invariants',
      },
      { label: 'Issue #48', href: `${GH}/issues/48` },
    ],
  },
  {
    id: 'frontend-documents',
    title: 'Frontend documents — collections, upload & viewer',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'Collections sidebar (list/create/rename/delete), multi-file drag-drop upload with live progress and pending→processing→ready→failed status polling, a filterable per-collection document list, a 302-following document viewer, and delete — with 413/415/failed/empty states handled.',
    links: [
      {
        label: 'Security invariants',
        href: '/docs/specs/0004-security-and-domain-invariants',
      },
      { label: 'Issue #49', href: `${GH}/issues/49` },
    ],
  },
  {
    id: 'frontend-chat',
    title: 'Frontend chat — streaming, citations, model picker & history',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'The chat slice on the auth foundation: send → subscribe to the WS stream_id → render delta tokens live with tool activity ("searching documents…"), clickable passage-level citations that open the cited document, an honest zero-citation answer, a grouped model picker (frontier/fast/oss), and a history sidebar with new/rename/delete. WS error + disconnect are terminal with retry.',
    links: [
      {
        label: 'Security invariants',
        href: '/docs/specs/0004-security-and-domain-invariants',
      },
      { label: 'Issue #50', href: `${GH}/issues/50` },
    ],
  },
  {
    id: 'dev-pages',
    title: 'In-app docs viewer & features catalog',
    area: 'Frontend',
    status: 'shipped',
    summary:
      'This page and the docs viewer, reachable from the main app via a floating hover overlay.',
    links: [
      { label: 'Open docs', href: '/docs' },
      { label: 'Issue #34', href: `${GH}/issues/34` },
    ],
  },
  {
    id: 'embeddings',
    title: 'Embeddings service',
    area: 'Retrieval',
    status: 'in-progress',
    summary:
      'Local OSS embeddings to unblock ingestion → retrieval; the gateway degrades cleanly until it lands.',
    links: [{ label: 'Issue #32', href: `${GH}/issues/32` }],
  },
  {
    id: 'grounded-chat',
    title: 'Grounded chat with citations',
    area: 'Retrieval',
    status: 'planned',
    summary:
      'The MVP: permissioned, cited, auditable answers over connected sources and uploaded documents.',
    links: [{ label: 'Scope spec', href: '/docs/specs/0003-product-scope-and-mission' }],
  },
  {
    id: 'security-invariants',
    title: 'Security & tenancy invariants',
    area: 'Security',
    status: 'planned',
    summary:
      'Permission, tenancy, identity and audit invariants (OD-4) — gates the M0 security cross-cuttings.',
    links: [
      { label: 'Open decisions', href: '/docs/specs/0001-open-decisions' },
      { label: 'Issue #14', href: `${GH}/issues/14` },
    ],
  },
];

export function isInternalLink(href: string): boolean {
  return href.startsWith('/');
}

function areaRank(area: string): number {
  const i = AREA_ORDER.indexOf(area);
  return i === -1 ? AREA_ORDER.length : i;
}

/** Group the catalog by area, ordered by AREA_ORDER then alphabetically. */
export function groupByArea(entries: readonly CatalogEntry[]): AreaGroup[] {
  const byArea = new Map<string, CatalogEntry[]>();
  for (const e of entries) {
    const arr = byArea.get(e.area);
    if (arr) arr.push(e);
    else byArea.set(e.area, [e]);
  }
  return [...byArea.entries()]
    .map(([area, group]) => ({ area, entries: group }))
    .sort((a, b) => areaRank(a.area) - areaRank(b.area) || (a.area < b.area ? -1 : 1));
}
