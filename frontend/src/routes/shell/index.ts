/**
 * App shell public surface (issue #110). The router composes the layout from
 * `AppShell`; the pieces are exported for tests and for any future host.
 */
export { AppShell } from './AppShell';
export { TopBar } from './TopBar';
export { NavRail } from './NavRail';
export { Brand } from './Brand';
export { TenantPill } from './TenantPill';
export { OmniBar } from './OmniBar';
export { AppearanceMenu } from './AppearanceMenu';
export { AccountMenu } from './AccountMenu';
export {
  buildRailGroups,
  RAIL_GROUPS,
  RAIL_ITEMS,
  RAIL_PATHS,
  type RailGroup,
  type RailLink,
  type RailGroupModel,
} from './navModel';
