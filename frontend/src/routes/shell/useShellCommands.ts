/**
 * Shell command-palette items (issue #110) — builds the navigation commands the
 * omni bar's palette runs, from the same shell rail model that drives the nav (so
 * the palette and the rail never drift). "Go to <screen>" for every available
 * rail destination, plus light/dark/system mode commands via the existing theme
 * hook. Consumed by the shell, which renders the existing `ui/CommandPalette`.
 */
import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useThemeMode, type CommandItem, type ThemeMode } from '@/ui';
import { featureNavItems, featureRoutes } from '@/routes/discovery';
import { buildRailGroups } from './navModel';

export function useShellCommands(): CommandItem[] {
  const navigate = useNavigate();
  const { setMode } = useThemeMode();

  return useMemo(() => {
    const groups = buildRailGroups(featureNavItems, featureRoutes);
    const navCommands: CommandItem[] = groups.flatMap((group) =>
      group.links
        .filter((link) => link.available)
        .map((link) => ({
          id: `nav:${link.to}`,
          label: `Go to ${link.label}`,
          keywords: `${group.label} ${link.to}`,
          icon: link.icon,
          hint: group.label,
          run: () => navigate(link.to),
        })),
    );

    const modes: Array<{ mode: ThemeMode; label: string }> = [
      { mode: 'light', label: 'Light' },
      { mode: 'dark', label: 'Dark' },
      { mode: 'system', label: 'System' },
    ];
    const modeCommands: CommandItem[] = modes.map(({ mode, label }) => ({
      id: `mode:${mode}`,
      label: `Mode: ${label}`,
      keywords: 'theme appearance dark light',
      icon: mode === 'light' ? 'sun' : mode === 'dark' ? 'moon' : 'monitor',
      hint: 'Appearance',
      run: () => setMode(mode),
    }));

    return [...navCommands, ...modeCommands];
  }, [navigate, setMode]);
}
