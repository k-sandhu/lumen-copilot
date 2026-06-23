/**
 * Left nav rail (issue #110) — the grouped navigation that REPLACES the floating
 * NavOverlay. Grouping is owned by the shell (`navModel.ts` path→group map); the
 * labels are resolved from the auto-discovered nav (`routes/discovery`
 * featureNavItems) so each feature keeps label ownership (ADR-0008 §3). Developer
 * pages (`/docs`, `/features`) are absent because they are not in the map.
 *
 * Active state highlights the current route (exact for `/`, prefix for the rest).
 * A path with no discovered route (e.g. `/sources`, pending #20/#27) renders as a
 * disabled "Soon" entry rather than a dead link. Icons come from `ui/Icon`.
 */
import { Link, useLocation } from 'react-router-dom';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';
import { featureNavItems, featureRoutes } from '@/routes/discovery';
import { buildRailGroups, type RailLink } from './navModel';

function isActive(pathname: string, to: string): boolean {
  if (to === '/') return pathname === '/';
  return pathname === to || pathname.startsWith(`${to}/`);
}

function RailItem({ link, active }: { link: RailLink; active: boolean }) {
  const body = (
    <>
      <Icon name={link.icon} />
      <span className="lc-navlink__label">{link.label}</span>
      {!link.available ? <span className="lc-navlink__soon">Soon</span> : null}
    </>
  );

  if (!link.available) {
    return (
      <span
        className="lc-navlink is-disabled"
        aria-disabled="true"
        title={`${link.label} — coming soon`}
      >
        {body}
      </span>
    );
  }

  return (
    <Link
      to={link.to}
      className={cn('lc-navlink', active && 'is-active')}
      aria-current={active ? 'page' : undefined}
    >
      {body}
    </Link>
  );
}

interface NavRailProps {
  /** Called when a link is followed — lets the mobile overlay close itself. */
  onNavigate?: () => void;
}

export function NavRail({ onNavigate }: NavRailProps) {
  const { pathname } = useLocation();
  const groups = buildRailGroups(featureNavItems, featureRoutes);

  return (
    <nav className="lc-shell__rail" aria-label="Primary" onClick={onNavigate}>
      {groups.map((group) => (
        <div key={group.label}>
          <div className="lc-rail__group-label">{group.label}</div>
          {group.links.map((link) => (
            <RailItem key={link.to} link={link} active={isActive(pathname, link.to)} />
          ))}
        </div>
      ))}
    </nav>
  );
}
