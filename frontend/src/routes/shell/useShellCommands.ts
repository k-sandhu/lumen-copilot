/**
 * Shell command-palette items (issues #110, #134) — builds the navigation
 * commands the omni bar's palette runs, from the same shell rail model that
 * drives the nav (so the palette and the rail never drift). Plus appearance
 * commands — theme, mode, and density — routed through the shared appearance
 * engine (useAppearance), so they stay in lock-step with the Appearance panel.
 */
import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppearance, THEMES, DENSITIES, type CommandItem, type ThemeMode } from '@/ui';
import { featureNavItems, featureRoutes } from '@/routes/discovery';
import { buildRailGroups } from './navModel';

export function useShellCommands(): CommandItem[] {
  const navigate = useNavigate();
  const { setMode, setTheme, setDensity } = useAppearance();

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
      keywords: 'theme appearance dark light mode',
      icon: mode === 'light' ? 'sun' : mode === 'dark' ? 'moon' : 'monitor',
      hint: 'Appearance',
      run: () => setMode(mode),
    }));

    const themeCommands: CommandItem[] = THEMES.map((t) => ({
      id: `theme:${t.id}`,
      label: `Theme: ${t.label}`,
      keywords: `appearance color ${t.id}`,
      icon: 'sparkles',
      hint: 'Appearance',
      run: () => setTheme(t.id),
    }));

    const densityCommands: CommandItem[] = DENSITIES.map((d) => ({
      id: `density:${d.id}`,
      label: `Density: ${d.label}`,
      keywords: 'appearance spacing compact comfortable size',
      icon: 'sliders',
      hint: 'Appearance',
      run: () => setDensity(d.id),
    }));

    return [...navCommands, ...modeCommands, ...themeCommands, ...densityCommands];
  }, [navigate, setMode, setTheme, setDensity]);
}
