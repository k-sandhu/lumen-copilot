/**
 * Pure presentation helpers for the artifacts slice (#222) — no I/O, no React.
 * Keeps formatting + preview-capability logic out of JSX so it can be unit-tested
 * directly. An artifact is a file an agent/run PRODUCED (CC-B #208), distinct from
 * an uploaded document; the panel lists, previews, and downloads them.
 */
import type { Artifact, ArtifactProducedBy } from '@/api';

/** Compact human-readable byte size (e.g. "1.2 MB"). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

/** A short type tag from a filename / mime (e.g. "CSV", "PNG"). */
export function fileKind(artifact: Pick<Artifact, 'filename' | 'mime_type'>): string {
  const ext = artifact.filename.includes('.') ? artifact.filename.split('.').pop() : undefined;
  if (ext) return ext.toUpperCase();
  const subtype = artifact.mime_type.split('/').pop() ?? '';
  return (subtype || 'FILE').toUpperCase();
}

/** Human label for the write origin. */
export function producedByLabel(origin: ArtifactProducedBy): string {
  switch (origin) {
    case 'chat_session':
      return 'Chat';
    case 'run':
      return 'Run';
    case 'tool':
      return 'Tool';
  }
}

/**
 * How the panel should preview an artifact given its declared MIME type.
 *
 * - `image` — raster/vector image the browser renders natively (png/jpg/gif/webp).
 *   svg is deliberately NOT here: an inline `<img>`-of-blob is safe for raster, but
 *   an SVG can carry script, so it falls to `download` (opened only via the
 *   sanitized download path, never rendered inline as active content).
 * - `text` — plain text / markdown / csv / json: rendered as sanitized text
 *   (markdown through the shared pipeline; csv/json/plain as escaped `<pre>`),
 *   NEVER as raw HTML.
 * - `download` — everything else (office docs, svg, html, unknown): a download
 *   card only. HTML is never rendered inline (it would be active content).
 */
export type PreviewKind = 'image' | 'text' | 'download';

const TEXT_MIME = new Set(['text/plain', 'text/markdown', 'text/csv', 'application/json']);

const IMAGE_MIME = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

/**
 * `text/*` subtypes that CAN carry active content — never previewed inline as
 * text (that would defeat the point). They fall to the download card.
 */
const ACTIVE_TEXT_MIME = new Set(['text/html', 'text/xml', 'text/xsl']);

/** The preview strategy for an artifact's declared MIME type (AC-2). */
export function previewKind(mimeType: string): PreviewKind {
  const mime = mimeType.split(';')[0]?.trim().toLowerCase() ?? '';
  if (IMAGE_MIME.has(mime)) return 'image';
  if (ACTIVE_TEXT_MIME.has(mime)) return 'download';
  if (TEXT_MIME.has(mime) || mime.startsWith('text/')) return 'text';
  return 'download';
}

/** True when this artifact's text preview should render as markdown (highlight/tables). */
export function isMarkdown(mimeType: string): boolean {
  const mime = mimeType.split(';')[0]?.trim().toLowerCase() ?? '';
  return mime === 'text/markdown';
}

/**
 * Relative-time label from an ISO stamp, e.g. "2h ago" / "3d ago" / "just now".
 * `now` is injectable so unit tests stay deterministic. Returns `null` for a
 * missing/unparseable stamp. (Mirrors the documents slice's helper — no shared
 * cross-feature import per frontend/AGENTS.md.)
 */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}
