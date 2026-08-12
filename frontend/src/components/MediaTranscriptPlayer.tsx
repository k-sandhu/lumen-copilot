/** Native audio/video player paired with a paginated, diarized transcript. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ApiError,
  createDocumentAccessUrl,
  fetchDocumentTranscript,
  type DocumentAccessUrl,
  type TranscriptPage,
  type TranscriptSegment,
  type TranscriptSpeaker,
} from '@/api';
import { cn } from '@/lib/cn';
import { formatMediaTimestamp } from '@/lib/mediaTime';

export interface MediaTranscriptPlayerProps {
  documentId: string;
  filename: string;
  kind: 'audio' | 'video';
  initialAccess: DocumentAccessUrl;
  /** Citation-relative initial seek; applying it never starts playback. */
  initialTimeMs?: number;
}

type TranscriptState =
  | { kind: 'loading' }
  | { kind: 'ready'; page: TranscriptPage }
  | { kind: 'processing' }
  | { kind: 'gone' }
  | { kind: 'error'; message: string };

interface PlaybackRestore {
  timeSeconds: number;
  shouldPlay: boolean;
}

const ACCESS_EXPIRY_SKEW_MS = 60_000;

export function MediaTranscriptPlayer({
  documentId,
  filename,
  kind,
  initialAccess,
  initialTimeMs,
}: MediaTranscriptPlayerProps) {
  const mediaRef = useRef<HTMLMediaElement>(null);
  const pendingSeekRef = useRef<number | null>(initialTimeMs ?? null);
  const restoreRef = useRef<PlaybackRestore | null>(null);
  const optimisticRefreshUsedRef = useRef(false);
  const refreshInFlightRef = useRef(false);
  const awaitingRefreshedMetadataRef = useRef(false);
  const [access, setAccess] = useState(initialAccess);
  const [refreshingAccess, setRefreshingAccess] = useState(false);
  const [accessError, setAccessError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    setTranscript({ kind: 'loading' });
    setPageError(null);
    void fetchDocumentTranscript(
      documentId,
      { limit: 100, ...(initialTimeMs !== undefined ? { around_ms: initialTimeMs } : {}) },
      abort.signal,
    )
      .then((page) => setTranscript({ kind: 'ready', page }))
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setTranscript({ kind: 'gone' });
        } else if (error instanceof ApiError && error.status === 409) {
          setTranscript({ kind: 'processing' });
        } else {
          setTranscript({
            kind: 'error',
            message:
              error instanceof ApiError ? error.displayMessage : 'Could not load transcript.',
          });
        }
      });
    return () => abort.abort();
  }, [attempt, documentId, initialTimeMs]);

  useEffect(() => {
    if (initialTimeMs === undefined) return;
    pendingSeekRef.current = initialTimeMs;
    seekWhenReady(mediaRef.current, initialTimeMs, pendingSeekRef);
  }, [initialTimeMs]);

  const speakers = useMemo(() => {
    if (transcript.kind !== 'ready') return new Map<string, TranscriptSpeaker>();
    return new Map(transcript.page.speakers.map((speaker) => [speaker.speaker_id, speaker]));
  }, [transcript]);

  const onLoadedMetadata = useCallback(() => {
    const media = mediaRef.current;
    if (!media) return;
    awaitingRefreshedMetadataRef.current = false;
    const restore = restoreRef.current;
    if (restore) {
      restoreRef.current = null;
      media.currentTime = clampSeek(media, restore.timeSeconds);
      if (restore.shouldPlay) void media.play().catch(() => undefined);
      return;
    }
    const target = pendingSeekRef.current;
    if (target !== null) {
      media.currentTime = clampSeek(media, target / 1000);
      pendingSeekRef.current = null;
    }
  }, []);

  const seek = useCallback((milliseconds: number) => {
    pendingSeekRef.current = milliseconds;
    seekWhenReady(mediaRef.current, milliseconds, pendingSeekRef);
  }, []);

  const refreshExpiredAccess = useCallback(
    async (manual = false) => {
      if (refreshInFlightRef.current) return;
      if (!manual && awaitingRefreshedMetadataRef.current) {
        setAccessError(
          'Playback could not continue. The file may use a codec this browser cannot play.',
        );
        return;
      }
      const nearExpiry = accessExpiresSoon(access.expires_at);
      if (!manual && !nearExpiry && optimisticRefreshUsedRef.current) {
        setAccessError(
          'Playback could not continue. The file may use a codec this browser cannot play.',
        );
        return;
      }
      if (!manual && !nearExpiry) optimisticRefreshUsedRef.current = true;

      refreshInFlightRef.current = true;
      const media = mediaRef.current;
      restoreRef.current = {
        timeSeconds: media?.currentTime ?? 0,
        shouldPlay: media ? !media.paused : false,
      };
      setRefreshingAccess(true);
      setAccessError(null);
      try {
        const next = await createDocumentAccessUrl(documentId, 'preview');
        awaitingRefreshedMetadataRef.current = true;
        setAccess(next);
      } catch (error) {
        awaitingRefreshedMetadataRef.current = false;
        restoreRef.current = null;
        setAccessError(
          error instanceof ApiError
            ? error.displayMessage
            : 'Playback access could not be refreshed.',
        );
      } finally {
        refreshInFlightRef.current = false;
        setRefreshingAccess(false);
      }
    },
    [access.expires_at, documentId],
  );

  const loadMore = useCallback(async () => {
    if (transcript.kind !== 'ready' || !transcript.page.next_cursor || loadingMore) return;
    setLoadingMore(true);
    setPageError(null);
    try {
      const next = await fetchDocumentTranscript(documentId, {
        cursor: transcript.page.next_cursor,
        limit: 100,
      });
      setTranscript((current) => {
        if (current.kind !== 'ready') return current;
        return {
          kind: 'ready',
          page: {
            ...current.page,
            speakers: mergeSpeakers(current.page.speakers, next.speakers),
            items: mergeSegments(current.page.items, next.items),
            next_cursor: next.next_cursor,
          },
        };
      });
    } catch (error) {
      setPageError(
        error instanceof ApiError ? error.displayMessage : 'Could not load more transcript.',
      );
    } finally {
      setLoadingMore(false);
    }
  }, [documentId, loadingMore, transcript]);

  const mediaProps = {
    ref: mediaRef as React.RefObject<HTMLAudioElement & HTMLVideoElement>,
    src: access.url,
    controls: true,
    preload: 'metadata' as const,
    className: kind === 'video' ? 'max-h-72 w-full bg-black' : 'w-full',
    'aria-label': `${kind === 'video' ? 'Video' : 'Audio'} player for ${filename}`,
    onLoadedMetadata,
    onError: () => void refreshExpiredAccess(),
    onTimeUpdate: (event: React.SyntheticEvent<HTMLMediaElement>) => {
      if (transcript.kind !== 'ready') return;
      const now = event.currentTarget.currentTime * 1000;
      const active = transcript.page.items.find(
        (segment) => now >= segment.start_ms && now < segment.end_ms,
      );
      setActiveSegmentId(active?.id ?? null);
    },
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface">
      <div className="shrink-0 border-b border-border bg-surface-muted/30 p-3">
        {kind === 'video' ? <video {...mediaProps} /> : <audio {...mediaProps} />}
        {refreshingAccess ? (
          <p role="status" className="mt-1 text-xs text-foreground-muted">
            Refreshing playback access…
          </p>
        ) : null}
        {accessError ? (
          <div
            role="alert"
            className="mt-2 flex items-center justify-between gap-2 text-xs text-danger"
          >
            <span>{accessError}</span>
            <button
              type="button"
              onClick={() => {
                void refreshExpiredAccess(true);
              }}
              className="rounded border border-border px-2 py-1 text-foreground hover:bg-surface-muted"
            >
              Retry playback
            </button>
          </div>
        ) : null}
      </div>

      <section aria-label="Timestamped transcript" className="flex min-h-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-border px-3 py-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-foreground-muted">
            Transcript
          </h3>
          {transcript.kind === 'ready' ? (
            <p className="text-[11px] text-foreground-muted">
              {transcript.page.language ?? 'Language not reported'} ·{' '}
              {formatDuration(transcript.page.duration_ms)}
            </p>
          ) : null}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          <TranscriptBody
            state={transcript}
            speakers={speakers}
            activeSegmentId={activeSegmentId}
            onSeek={seek}
            onRetry={() => setAttempt((value) => value + 1)}
          />
          {transcript.kind === 'ready' && transcript.page.next_cursor ? (
            <div className="mt-2 text-center">
              <button
                type="button"
                disabled={loadingMore}
                onClick={() => void loadMore()}
                className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-surface-muted disabled:opacity-60"
              >
                {loadingMore ? 'Loading…' : 'Load more transcript'}
              </button>
              {pageError ? (
                <p role="alert" className="mt-1 text-xs text-danger">
                  {pageError}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function TranscriptBody({
  state,
  speakers,
  activeSegmentId,
  onSeek,
  onRetry,
}: {
  state: TranscriptState;
  speakers: Map<string, TranscriptSpeaker>;
  activeSegmentId: string | null;
  onSeek: (milliseconds: number) => void;
  onRetry: () => void;
}) {
  if (state.kind === 'loading') {
    return (
      <p role="status" className="p-4 text-center text-sm text-foreground-muted">
        Loading transcript…
      </p>
    );
  }
  if (state.kind === 'gone') {
    return (
      <p role="alert" className="p-4 text-center text-sm text-foreground-muted">
        This media is no longer available.
      </p>
    );
  }
  if (state.kind === 'processing') {
    return <StateMessage message="The transcript is still being prepared." onRetry={onRetry} />;
  }
  if (state.kind === 'error') {
    return <StateMessage message={state.message} onRetry={onRetry} danger />;
  }
  if (state.page.items.length === 0) {
    return (
      <p role="status" className="p-4 text-center text-sm text-foreground-muted">
        No speech was detected in this media.
      </p>
    );
  }
  return (
    <ol className="space-y-1" aria-label="Transcript segments">
      {state.page.items.map((segment) => {
        const speaker = speakers.get(segment.speaker_id);
        return (
          <li
            key={segment.id}
            data-active={activeSegmentId === segment.id || undefined}
            className={cn(
              'grid grid-cols-[4.5rem_1fr] gap-2 rounded-md p-2 text-sm',
              activeSegmentId === segment.id
                ? 'bg-accent/15 ring-1 ring-accent/40'
                : 'hover:bg-surface-muted/60',
            )}
          >
            <button
              type="button"
              onClick={() => onSeek(segment.start_ms)}
              className="self-start rounded px-1 py-0.5 font-mono text-xs text-accent hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Seek to ${formatMediaTimestamp(segment.start_ms)}`}
            >
              {formatMediaTimestamp(segment.start_ms)}
            </button>
            <div className="min-w-0">
              <p className="text-xs font-semibold text-foreground">
                {speakerLabel(segment.speaker_id, speaker)}
                {speaker?.name_status === 'inferred' ? (
                  <span
                    className="ml-1 font-normal text-foreground-muted"
                    title="Inferred from explicit dialogue in this transcript; not voice recognition"
                  >
                    (inferred)
                  </span>
                ) : null}
              </p>
              <p className="mt-0.5 whitespace-pre-wrap break-words leading-relaxed">
                {segment.text}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function StateMessage({
  message,
  onRetry,
  danger = false,
}: {
  message: string;
  onRetry: () => void;
  danger?: boolean;
}) {
  return (
    <div role={danger ? 'alert' : 'status'} className="p-4 text-center">
      <p className={cn('text-sm', danger ? 'text-danger' : 'text-foreground-muted')}>{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-2 rounded-md border border-border px-3 py-1.5 text-xs hover:bg-surface-muted"
      >
        Retry
      </button>
    </div>
  );
}

function speakerLabel(speakerId: string, speaker?: TranscriptSpeaker): string {
  if (speaker?.name_status === 'inferred' && speaker.display_name) return speaker.display_name;
  const match = /^speaker-(\d+)$/.exec(speakerId);
  return match ? `Speaker ${match[1]}` : speakerId;
}

function formatDuration(milliseconds: number): string {
  return formatMediaTimestamp(milliseconds);
}

function seekWhenReady(
  media: HTMLMediaElement | null,
  milliseconds: number,
  pending: React.MutableRefObject<number | null>,
): void {
  if (!media || media.readyState < HTMLMediaElement.HAVE_METADATA) return;
  media.currentTime = clampSeek(media, milliseconds / 1000);
  pending.current = null;
}

function clampSeek(media: HTMLMediaElement, seconds: number): number {
  const max = Number.isFinite(media.duration) && media.duration > 0 ? media.duration : seconds;
  return Math.max(0, Math.min(seconds, max));
}

function accessExpiresSoon(expiresAt: string): boolean {
  const expiry = Date.parse(expiresAt);
  return Number.isFinite(expiry) && expiry <= Date.now() + ACCESS_EXPIRY_SKEW_MS;
}

function mergeSpeakers(
  current: TranscriptSpeaker[],
  next: TranscriptSpeaker[],
): TranscriptSpeaker[] {
  const byId = new Map(current.map((speaker) => [speaker.speaker_id, speaker]));
  for (const speaker of next) byId.set(speaker.speaker_id, speaker);
  return [...byId.values()];
}

function mergeSegments(
  current: TranscriptSegment[],
  next: TranscriptSegment[],
): TranscriptSegment[] {
  const byId = new Map(current.map((segment) => [segment.id, segment]));
  for (const segment of next) byId.set(segment.id, segment);
  return [...byId.values()].sort((left, right) => left.ordinal - right.ordinal);
}
