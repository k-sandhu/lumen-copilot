/**
 * ModeToggle — the v1 appearance control (ADR-0007 §2): light / dark / system.
 * A segmented control of three pressable buttons; the system option follows the
 * OS via prefers-color-scheme (handled in theme/mode.ts). Driven by useThemeMode
 * by default, but a controlled mode/onChange pair can be supplied for tests or
 * for hosting in an external store.
 *
 * A11y: a labelled group of toggle buttons; the active option carries
 * aria-pressed="true" so assistive tech announces the selection.
 */
import { cn } from '@/lib/cn';
import { Icon, type IconName } from './Icon';
import { useThemeMode } from './theme/useThemeMode';
import type { ThemeMode } from './theme/mode';

interface ModeToggleProps {
  /** Controlled value. Omit to self-manage via useThemeMode. */
  mode?: ThemeMode;
  onChange?: (mode: ThemeMode) => void;
  className?: string;
}

const OPTIONS: Array<{ value: ThemeMode; label: string; icon: IconName }> = [
  { value: 'light', label: 'Light', icon: 'sun' },
  { value: 'dark', label: 'Dark', icon: 'moon' },
  { value: 'system', label: 'System', icon: 'monitor' },
];

export function ModeToggle({ mode, onChange, className }: ModeToggleProps) {
  const managed = useThemeMode();
  const current = mode ?? managed.mode;
  const set = onChange ?? managed.setMode;

  return (
    <div className={cn('lc-modetoggle', className)} role="group" aria-label="Appearance mode">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          aria-pressed={current === opt.value}
          aria-label={opt.label}
          onClick={() => set(opt.value)}
        >
          <Icon name={opt.icon} />
          <span>{opt.label}</span>
        </button>
      ))}
    </div>
  );
}
