/**
 * Shell nav model (issue #110) — the path→group map that OWNS the grouping of
 * the left rail. Grouping lives HERE, in the shell, not in each feature's
 * `nav.ts` (ADR-0008 §1 keeps feature files append-free; the shell is the single
 * place that decides which group a path belongs to).
 *
 * The rail is assembled by RESOLVING each defined item against the
 * auto-discovered nav (`routes/discovery` featureNavItems) for its label — so a
 * feature still owns its own label — while the shell owns icon + group + order.
 * Items NOT listed here (the developer pages `/docs`, `/features`) are excluded
 * from the rail by construction. An item whose route is not yet discovered (e.g.
 * `/sources`, pending the connector framework #20/#27) renders as a disabled
 * "coming soon" rail entry rather than a dead link.
 */
import type { IconName } from '@/ui';

/** The two rail groups, in render order. */
export const RAIL_GROUPS = ['Workspace', 'Administration'] as const;
export type RailGroup = (typeof RAIL_GROUPS)[number];

/** A rail entry the shell defines by path. Label is resolved from discovery. */
export interface RailItemSpec {
  /** Destination route path (matches a discovered feature route when present). */
  to: string;
  /** Which group it renders under. */
  group: RailGroup;
  /** Rail icon (shell-owned, from the kit's `ui/Icon`). */
  icon: IconName;
  /** Fallback label when no discovered nav entry supplies one. */
  fallbackLabel: string;
  /** Order within its group (lower first). */
  order: number;
}

/**
 * The shell-owned rail map. Path → group, icon, order. Labels prefer the
 * feature's own discovered nav entry (so the feature keeps label ownership) and
 * fall back to `fallbackLabel` for paths that have no `nav.ts` yet (Sources).
 */
export const RAIL_ITEMS: readonly RailItemSpec[] = [
  // Workspace
  { to: '/', group: 'Workspace', icon: 'message-square', fallbackLabel: 'Assistant', order: 0 },
  { to: '/search', group: 'Workspace', icon: 'search', fallbackLabel: 'Search', order: 1 },
  { to: '/documents', group: 'Workspace', icon: 'file-text', fallbackLabel: 'Documents', order: 2 },
  // Administration
  { to: '/sources', group: 'Administration', icon: 'plug', fallbackLabel: 'Sources', order: 0 },
  {
    to: '/audit',
    group: 'Administration',
    icon: 'shield-check',
    fallbackLabel: 'Audit log',
    order: 1,
  },
  { to: '/admin', group: 'Administration', icon: 'sliders', fallbackLabel: 'Admin', order: 2 },
] as const;

/** One resolved rail link ready to render. */
export interface RailLink {
  to: string;
  label: string;
  icon: IconName;
  group: RailGroup;
  order: number;
  /** False when no discovered route backs this path (render disabled). */
  available: boolean;
}

/** One resolved group with its links, in render order. */
export interface RailGroupModel {
  label: RailGroup;
  links: RailLink[];
}

interface DiscoveredNav {
  to: string;
  label: string;
}

interface DiscoveredRoute {
  path: string;
}

/** Treat a `/docs/*` splat route as backing the `/docs` root, etc. */
function routeBacks(routePath: string, to: string): boolean {
  return routePath.replace(/\/\*$/, '') === to;
}

/**
 * Resolve the rail groups from the shell map + the auto-discovered nav/routes.
 * Pure (no React) so the grouping is unit-testable in isolation.
 */
export function buildRailGroups(
  discoveredNav: readonly DiscoveredNav[],
  discoveredRoutes: readonly DiscoveredRoute[],
): RailGroupModel[] {
  const labelFor = (to: string, fallback: string): string =>
    discoveredNav.find((n) => n.to === to)?.label ?? fallback;
  const isAvailable = (to: string): boolean => discoveredRoutes.some((r) => routeBacks(r.path, to));

  return RAIL_GROUPS.map((group) => ({
    label: group,
    links: RAIL_ITEMS.filter((item) => item.group === group)
      .slice()
      .sort((a, b) => a.order - b.order)
      .map((item) => ({
        to: item.to,
        label: labelFor(item.to, item.fallbackLabel),
        icon: item.icon,
        group,
        order: item.order,
        available: isAvailable(item.to),
      })),
  }));
}

/** The set of paths the rail renders — everything else (dev pages) is excluded. */
export const RAIL_PATHS: ReadonlySet<string> = new Set(RAIL_ITEMS.map((i) => i.to));
