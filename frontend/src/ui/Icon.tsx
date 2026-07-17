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
  | 'filter'
  | 'sliders'
  | 'bell'
  | 'sparkles'
  | 'plug'
  | 'plus'
  | 'send'
  | 'pencil'
  | 'trash'
  | 'play'
  | 'pause'
  | 'settings'
  | 'calendar'
  | 'inbox'
  | 'globe'
  | 'link'
  | 'package'
  | 'at-sign';

const PATHS: Record<IconName, string> = {
  package: 'M21 8 12 3 3 8v8l9 5 9-5V8ZM3 8l9 5m0 0 9-5m-9 5v8',
  'at-sign': 'M12 16a4 4 0 1 1 4-4v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1',
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
  sliders:
    'M4 6h10m4 0h2M14 6a2 2 0 1 0 4 0 2 2 0 0 0-4 0ZM4 18h2m4 0h10M6 18a2 2 0 1 0 4 0 2 2 0 0 0-4 0Z',
  bell: 'M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M10.3 21a1.9 1.9 0 0 0 3.4 0',
  sparkles:
    'M12 3l1.6 4.6L18 9l-4.4 1.4L12 15l-1.6-4.6L6 9l4.4-1.4L12 3ZM19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z',
  plug: 'M9 3v5m6-5v5M6 8h12v2a6 6 0 0 1-12 0V8ZM12 16v5',
  plus: 'M12 5v14M5 12h14',
  send: 'M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z',
  pencil: 'M4 20h4L19 9l-4-4L4 16v4ZM14 6l4 4',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
  play: 'M6 4l14 8-14 8V4Z',
  pause: 'M8 5v14M16 5v14',
  settings:
    'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.6 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z',
  calendar: 'M7 3v4M17 3v4M4 8h16M5 6h14v14H5V6Z',
  inbox:
    'M4 13h4l2 3h4l2-3h4M4 13 6 5h12l2 8M4 13v6h16v-6',
  // A web/internet globe — the trust glyph for a web-sourced citation (#221).
  globe: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3 12h18M12 3c2.5 2.5 3.5 5.5 3.5 9S14.5 18.5 12 21c-2.5-2.5-3.5-5.5-3.5-9S9.5 5.5 12 3Z',
  // A chain link — the external-link affordance on a web citation (#221).
  link: 'M9 15l6-6M10.5 6.5 12 5a4 4 0 0 1 6 6l-1.5 1.5M13.5 17.5 12 19a4 4 0 0 1-6-6l1.5-1.5',
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
