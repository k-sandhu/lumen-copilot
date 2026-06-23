/**
 * Lumen Copilot trust-signal UI kit (issue #81, ADR-0007 §2).
 *
 * The kit is OPT-IN: importing from `@/ui` pulls in the design-system token
 * layer + component styles via the side-effect CSS imports below, so existing
 * screens that don't import the kit are unaffected. The token layer is additive
 * to styles/index.css (the existing Tailwind RGB-triple tokens); the mode
 * provider keeps [data-mode] and the legacy `.dark` class in lock-step.
 *
 * v1 appearance scope (curated): Aurora theme + light/dark/system mode +
 * command palette. The 7-theme picker, accent override, and density/font axes
 * are deferred — the token architecture supports them, but no switching UI ships.
 */
import '@/styles/tokens.css';
import '@/styles/ui-kit.css';

// Trust signals
export { PermissionPill, type PermissionLevel } from './PermissionPill';
export { FreshnessPill } from './FreshnessPill';
export { StatusDot, type StatusTone } from './StatusDot';
export { RiskTierBadge, type RiskTier } from './RiskTierBadge';
export { KpiCard, type KpiTrend } from './KpiCard';
export { CitationChip } from './CitationChip';
export { SourceInspector, type SourcePassage, type SourceInspectorProps } from './SourceInspector';
export { RetrievalTrace, type TraceStep } from './RetrievalTrace';
export { AuditRow, type AuditEvent, type AuditKind } from './AuditRow';
export {
  ProvenanceDrawer,
  type ProvenanceDetail,
  type ProvenanceCandidate,
} from './ProvenanceDrawer';

// Appearance + navigation
export { ModeToggle } from './ModeToggle';
export { CommandPalette, type CommandItem } from './CommandPalette';
export { useCommandPalette, type CommandPaletteApi } from './useCommandPalette';
export { useThemeMode, type ThemeModeApi } from './theme/useThemeMode';
export {
  THEME_NAME,
  type ThemeMode,
  type ResolvedMode,
  readStoredMode,
  resolveMode,
  applyAppearance,
} from './theme/mode';

// Primitives
export { Icon, type IconName } from './Icon';
