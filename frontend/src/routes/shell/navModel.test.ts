/**
 * Shell nav model (issue #110) — proves the path→group grouping the shell OWNS:
 * the two groups in order, dev pages excluded, labels resolved from the
 * discovered nav, and a path with no backing route flagged unavailable ("Soon").
 */
import { describe, it, expect } from 'vitest';
import { buildRailGroups, RAIL_GROUPS, RAIL_PATHS } from './navModel';

const DISCOVERED_NAV = [
  { to: '/search', label: 'Search' },
  { to: '/documents', label: 'Documents' },
  { to: '/assistants', label: 'Assistants' },
  { to: '/schedules', label: 'Schedules' },
  { to: '/runs', label: 'Run history' },
  { to: '/artifacts', label: 'Artifacts' },
  { to: '/mcp-servers', label: 'MCP servers' },
  { to: '/audit', label: 'Audit log' },
  { to: '/admin', label: 'Admin' },
  // dev-page nav entries that MUST NOT leak into the rail
  { to: '/docs', label: 'Documentation' },
  { to: '/features', label: 'Features built' },
];

const DISCOVERED_ROUTES = [
  { path: '/' },
  { path: '/search' },
  { path: '/documents' },
  { path: '/assistants' },
  { path: '/schedules' },
  { path: '/runs' },
  { path: '/artifacts' },
  { path: '/mcp-servers' },
  { path: '/audit' },
  { path: '/admin' },
  { path: '/docs/*' },
  { path: '/features' },
  // note: no /sources route — it should render disabled
];

describe('buildRailGroups', () => {
  it('renders exactly the two groups, in order', () => {
    const groups = buildRailGroups(DISCOVERED_NAV, DISCOVERED_ROUTES);
    expect(groups.map((g) => g.label)).toEqual([...RAIL_GROUPS]);
  });

  it('groups every shipped screen: Workspace (chat→artifacts) and Administration (sources→admin) (#374)', () => {
    const groups = buildRailGroups(DISCOVERED_NAV, DISCOVERED_ROUTES);
    const workspace = groups.find((g) => g.label === 'Workspace');
    const admin = groups.find((g) => g.label === 'Administration');
    expect(workspace?.links.map((l) => l.to)).toEqual([
      '/',
      '/search',
      '/documents',
      '/assistants',
      '/schedules',
      '/runs',
      '/artifacts',
    ]);
    expect(admin?.links.map((l) => l.to)).toEqual(['/sources', '/mcp-servers', '/audit', '/admin']);
  });

  it('excludes the developer pages (/docs, /features) from the rail', () => {
    const groups = buildRailGroups(DISCOVERED_NAV, DISCOVERED_ROUTES);
    const allTargets = groups.flatMap((g) => g.links.map((l) => l.to));
    expect(allTargets).not.toContain('/docs');
    expect(allTargets).not.toContain('/features');
    expect(RAIL_PATHS.has('/docs')).toBe(false);
    expect(RAIL_PATHS.has('/features')).toBe(false);
  });

  it('resolves labels from the discovered nav, falling back to the shell default', () => {
    const groups = buildRailGroups(DISCOVERED_NAV, DISCOVERED_ROUTES);
    const links = groups.flatMap((g) => g.links);
    // resolved from discovery
    expect(links.find((l) => l.to === '/audit')?.label).toBe('Audit log');
    expect(links.find((l) => l.to === '/runs')?.label).toBe('Run history');
    expect(links.find((l) => l.to === '/mcp-servers')?.label).toBe('MCP servers');
    // fallback: '/' has no nav.ts, and '/sources' has no nav entry yet
    expect(links.find((l) => l.to === '/')?.label).toBe('Assistant');
    expect(links.find((l) => l.to === '/sources')?.label).toBe('Sources');
  });

  it('flags a path with no backing route as unavailable (Sources, pending #20/#27)', () => {
    const groups = buildRailGroups(DISCOVERED_NAV, DISCOVERED_ROUTES);
    const links = groups.flatMap((g) => g.links);
    expect(links.find((l) => l.to === '/sources')?.available).toBe(false);
    // every real screen is available
    expect(links.find((l) => l.to === '/audit')?.available).toBe(true);
    expect(links.find((l) => l.to === '/')?.available).toBe(true);
  });

  it('treats a splat route (/docs/*) as backing its root for availability', () => {
    // /admin is backed by an exact route; confirm the splat-normalization path too
    const groups = buildRailGroups(DISCOVERED_NAV, [{ path: '/admin/*' }]);
    const admin = groups.flatMap((g) => g.links).find((l) => l.to === '/admin');
    expect(admin?.available).toBe(true);
  });
});
