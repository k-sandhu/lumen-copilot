/**
 * Inline SVG icon set for the kit — a small, dependency-free sprite (no network,
 * works under `file://` and in tests). 1.7–2px stroke, scales with --fs via the
 * `.lc-icon` class. Only the glyphs the trust kit needs are included.
 */
import type { SVGProps } from 'react';
import { cn } from '@/lib/cn';

export type IconName =
  | 'shield-check'
  | 'lock'
  | 'shield-x'
  | 'clock'
  | 'check'
  | 'x'
  | 'minus'
  | 'search'
  | 'chevron-down'
  | 'arrow-up-right'
  | 'sun'
  | 'moon'
  | 'monitor'
  | 'trending-up'
  | 'trending-down'
  | 'file-text'
  | 'alert-triangle'
  | 'eye'
  | 'user'
  | 'message-square'
  | 'database'
  | 'list'
  | 'corner-down-left'
  | 'filter';

const PATHS: Record<IconName, string> = {
  'shield-check': 'M12 3 4 6v6c0 4.5 3.3 7.5 8 9 4.7-1.5 8-4.5 8-9V6l-8-3ZM9 12l2 2 4-4',
  lock: 'M6 10V8a6 6 0 0 1 12 0v2M5 10h14v10H5V10Z',
  'shield-x': 'M12 3 4 6v6c0 4.5 3.3 7.5 8 9 4.7-1.5 8-4.5 8-9V6l-8-3ZM9.5 9.5l5 5m0-5-5 5',
  clock: 'M12 7v5l3 2M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18Z',
  check: 'm5 12 5 5 9-11',
  x: 'M6 6l12 12M18 6 6 18',
  minus: 'M5 12h14',
  search: 'm21 21-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z',
  'chevron-down': 'm6 9 6 6 6-6',
  'arrow-up-right': 'M7 17 17 7M9 7h8v8',
  sun: 'M12 4V2m0 20v-2M4 12H2m20 0h-2M6 6 4.5 4.5M19.5 19.5 18 18M18 6l1.5-1.5M4.5 19.5 6 18M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z',
  moon: 'M21 12.8A8.5 8.5 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8Z',
  monitor: 'M4 5h16v11H4V5ZM8 20h8M12 16v4',
  'trending-up': 'm3 17 6-6 4 4 8-8M21 7v5m0-5h-5',
  'trending-down': 'm3 7 6 6 4-4 8 8M21 17v-5m0 5h-5',
  'file-text': 'M14 3H6v18h12V7l-4-4ZM14 3v4h4M9 13h6M9 17h6',
  'alert-triangle': 'M12 3 2 20h20L12 3ZM12 10v4m0 3h.01',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0',
  'message-square': 'M21 12a7 7 0 0 1-7 7H7l-4 3V12a7 7 0 0 1 7-7h4a7 7 0 0 1 7 7Z',
  database:
    'M12 7c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3ZM4 4v6c0 1.7 3.6 3 8 3s8-1.3 8-3V4M4 10v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  'corner-down-left': 'M9 10 4 15l5 5M4 15h12a4 4 0 0 0 4-4V4',
  filter: 'M3 4h18l-7 8v7l-4 2v-9L3 4Z',
};

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  className?: string;
  /** Most kit icons are decorative; set a title to make one labelled. */
  title?: string;
}

export function Icon({ name, className, title, ...rest }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={cn('lc-icon', className)}
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      <path d={PATHS[name]} />
    </svg>
  );
}
