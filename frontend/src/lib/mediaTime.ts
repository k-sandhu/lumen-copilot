/** Format a zero-based media timestamp without locale-dependent output. */
export function formatMediaTimestamp(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export interface MediaTimeSpan {
  startMs: number;
  endMs: number;
}

/** Fail closed on nullable/malformed REST media pairs before any seek UI opens. */
export function validMediaTimeSpan(
  startMs: number | null | undefined,
  endMs: number | null | undefined,
): MediaTimeSpan | null {
  if (
    typeof startMs !== 'number' ||
    typeof endMs !== 'number' ||
    !Number.isInteger(startMs) ||
    !Number.isInteger(endMs) ||
    startMs < 0 ||
    endMs <= startMs
  ) {
    return null;
  }
  return { startMs, endMs };
}
