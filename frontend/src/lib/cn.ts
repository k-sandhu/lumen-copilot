/** Tiny class-name joiner — drops falsy values, joins with spaces.
 * Kept dependency-free (no clsx) for the skeleton. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
