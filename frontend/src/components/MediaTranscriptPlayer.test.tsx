import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/api';
import { MediaTranscriptPlayer } from './MediaTranscriptPlayer';

const access: api.DocumentAccessUrl = {
  url: 'https://storage.example/media?one',
  filename: 'meeting.mp3',
  mime_type: 'audio/mpeg',
  size_bytes: 100,
  expires_at: '2030-01-01T00:00:00Z',
  purpose: 'preview',
  supports_byte_ranges: true,
};

const page: api.TranscriptPage = {
  document_id: 'doc-1',
  duration_ms: 60_000,
  language: 'en',
  transcription_model: 'x-ai/grok-stt-1.0',
  speakers: [
    {
      speaker_id: 'speaker-1',
      display_name: 'John',
      name_status: 'inferred',
      name_confidence: 0.95,
      name_method: 'self_introduction',
      evidence_segment_ids: ['seg-1'],
    },
    {
      speaker_id: 'speaker-2',
      display_name: null,
      name_status: 'unknown',
      name_confidence: null,
      name_method: null,
      evidence_segment_ids: [],
    },
  ],
  items: [
    {
      id: 'seg-1',
      ordinal: 0,
      speaker_id: 'speaker-1',
      start_ms: 12_500,
      end_ms: 18_000,
      char_start: 0,
      char_end: 22,
      text: 'Hello, my name is John.',
      confidence: 0.98,
    },
    {
      id: 'seg-2',
      ordinal: 1,
      speaker_id: 'speaker-2',
      start_ms: 18_000,
      end_ms: 22_000,
      char_start: 23,
      char_end: 34,
      text: 'Welcome.',
    },
  ],
  next_cursor: null,
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'fetchDocumentTranscript').mockResolvedValue(page);
});

describe('MediaTranscriptPlayer', () => {
  it.each([
    { source: 'citation', paused: true },
    { source: 'citation', paused: false },
    { source: 'transcript', paused: true },
    { source: 'transcript', paused: false },
  ] as const)(
    'keeps the latest $source seek across a pending refresh (paused=$paused)',
    async ({ source, paused }) => {
      let resolveRefresh!: (value: api.DocumentAccessUrl) => void;
      const refresh = new Promise<api.DocumentAccessUrl>((resolve) => {
        resolveRefresh = resolve;
      });
      const createAccess = vi.spyOn(api, 'createDocumentAccessUrl').mockReturnValue(refresh);
      const props = {
        documentId: 'doc-1',
        filename: 'meeting.mp3',
        kind: 'audio' as const,
        initialAccess: { ...access, expires_at: '2000-01-01T00:00:00Z' },
      };
      const { rerender } = render(<MediaTranscriptPlayer {...props} initialTimeMs={5_000} />);
      await screen.findByText('Welcome.');
      const player = screen.getByLabelText('Audio player for meeting.mp3') as HTMLAudioElement;
      Object.defineProperty(player, 'readyState', {
        configurable: true,
        value: HTMLMediaElement.HAVE_METADATA,
      });
      Object.defineProperty(player, 'duration', { configurable: true, value: 60 });
      Object.defineProperty(player, 'paused', { configurable: true, value: paused });
      const play = vi.spyOn(player, 'play').mockResolvedValue();
      fireEvent.loadedMetadata(player);
      expect(player.currentTime).toBe(5);
      fireEvent.error(player);
      expect(createAccess).toHaveBeenCalledTimes(1);

      const expectedTime = source === 'citation' ? 50 : 18;
      if (source === 'citation') {
        rerender(<MediaTranscriptPlayer {...props} initialTimeMs={50_000} />);
      } else {
        await userEvent.setup().click(screen.getByRole('button', { name: 'Seek to 0:18' }));
      }
      expect(player.currentTime).toBe(expectedTime);
      const refreshed = { ...access, url: 'https://storage.example/media?refreshed' };
      await act(async () => {
        resolveRefresh(refreshed);
        await refresh;
      });
      expect(player).toHaveAttribute('src', refreshed.url);
      // Browsers reset the source timeline until the new metadata arrives.
      player.currentTime = 0;
      fireEvent.loadedMetadata(player);
      expect(player.currentTime).toBe(expectedTime);
      expect(play).toHaveBeenCalledTimes(paused ? 0 : 1);
    },
  );

  it.each(['success', 'error'] as const)(
    'ignores stale pagination %s after a new citation window',
    async (outcome) => {
      let resolveOld!: (value: api.TranscriptPage) => void;
      let rejectOld!: (error: Error) => void;
      const oldRequest = new Promise<api.TranscriptPage>((resolve, reject) => {
        resolveOld = resolve;
        rejectOld = reject;
      });
      const newWindow = {
        ...page,
        items: page.items.slice(0, 1).map((segment) => ({
          ...segment,
          id: 'new-turn',
          text: 'New citation turn',
          start_ms: 50_000,
          end_ms: 55_000,
        })),
        next_cursor: 'cursor-after-new-window',
      };
      const fetch = vi
        .spyOn(api, 'fetchDocumentTranscript')
        .mockResolvedValueOnce({ ...page, next_cursor: 'cursor-early' })
        .mockReturnValueOnce(oldRequest)
        .mockResolvedValueOnce(newWindow)
        .mockResolvedValueOnce({ ...newWindow, items: [], next_cursor: null });
      const props = {
        documentId: 'doc-1',
        filename: 'meeting.mp3',
        kind: 'audio' as const,
        initialAccess: access,
      };
      const { rerender } = render(<MediaTranscriptPlayer {...props} initialTimeMs={0} />);
      const user = userEvent.setup();
      await user.click(await screen.findByRole('button', { name: 'Load more transcript' }));
      expect(fetch.mock.calls[1]?.[1]?.cursor).toBe('cursor-early');
      rerender(<MediaTranscriptPlayer {...props} initialTimeMs={50_000} />);
      await screen.findByText('New citation turn');
      await act(async () => {
        if (outcome === 'success') {
          resolveOld({ ...page, next_cursor: 'cursor-stale' });
        } else {
          rejectOld(new Error('old pagination failed'));
        }
        await oldRequest.catch(() => undefined);
      });
      expect(screen.queryByText('Hello, my name is John.')).not.toBeInTheDocument();
      expect(screen.queryByText('Could not load more transcript.')).not.toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Load more transcript' }));
      expect(fetch.mock.calls[3]?.[1]?.cursor).toBe('cursor-after-new-window');
    },
  );

  it('uses native metadata-only audio and labels inferred versus neutral speakers', async () => {
    render(
      <MediaTranscriptPlayer
        documentId="doc-1"
        filename="meeting.mp3"
        kind="audio"
        initialAccess={access}
      />,
    );

    const player = screen.getByLabelText('Audio player for meeting.mp3');
    expect(player).toHaveAttribute('preload', 'metadata');
    expect(player).toHaveAttribute('src', access.url);
    expect(await screen.findByText('John')).toBeInTheDocument();
    expect(screen.getByText('(inferred)')).toHaveAttribute(
      'title',
      expect.stringMatching(/not voice recognition/i),
    );
    expect(screen.getByText('Speaker 2')).toBeInTheDocument();
  });

  it('queues a transcript seek until media metadata is available without autoplay', async () => {
    const user = userEvent.setup();
    render(
      <MediaTranscriptPlayer
        documentId="doc-1"
        filename="meeting.mp3"
        kind="audio"
        initialAccess={access}
      />,
    );
    await user.click(await screen.findByRole('button', { name: 'Seek to 0:12' }));
    const player = screen.getByLabelText('Audio player for meeting.mp3') as HTMLAudioElement;
    const play = vi.spyOn(player, 'play').mockResolvedValue();
    Object.defineProperty(player, 'readyState', {
      configurable: true,
      value: HTMLMediaElement.HAVE_METADATA,
    });
    Object.defineProperty(player, 'duration', { configurable: true, value: 60 });
    fireEvent.loadedMetadata(player);

    expect(player.currentTime).toBe(12.5);
    expect(play).not.toHaveBeenCalled();
  });

  it('renews each later URL expiry after metadata succeeds and preserves playback state', async () => {
    let now = Date.parse('2026-08-12T04:00:00Z');
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const expiredAccess = {
      ...access,
      expires_at: new Date(now - 1_000).toISOString(),
    };
    const nextAccess = {
      ...access,
      url: 'https://storage.example/media?two',
      expires_at: new Date(now + 120_000).toISOString(),
    };
    const laterAccess = {
      ...access,
      url: 'https://storage.example/media?three',
      expires_at: new Date(now + 300_000).toISOString(),
    };
    vi.spyOn(api, 'createDocumentAccessUrl')
      .mockResolvedValueOnce(nextAccess)
      .mockResolvedValueOnce(laterAccess);
    render(
      <MediaTranscriptPlayer
        documentId="doc-1"
        filename="meeting.mp3"
        kind="audio"
        initialAccess={expiredAccess}
      />,
    );
    const player = screen.getByLabelText('Audio player for meeting.mp3') as HTMLAudioElement;
    player.currentTime = 7;
    Object.defineProperty(player, 'paused', { configurable: true, value: false });
    const play = vi.spyOn(player, 'play').mockResolvedValue();
    fireEvent.error(player);
    await waitFor(() => expect(player).toHaveAttribute('src', nextAccess.url));
    fireEvent.error(player);
    expect(api.createDocumentAccessUrl).toHaveBeenCalledTimes(1);
    Object.defineProperty(player, 'readyState', {
      configurable: true,
      value: HTMLMediaElement.HAVE_METADATA,
    });
    Object.defineProperty(player, 'duration', { configurable: true, value: 60 });
    fireEvent.loadedMetadata(player);

    expect(player.currentTime).toBe(7);
    expect(play).toHaveBeenCalledTimes(1);

    now += 180_000;
    player.currentTime = 23;
    fireEvent.error(player);
    await waitFor(() => expect(player).toHaveAttribute('src', laterAccess.url));
    fireEvent.loadedMetadata(player);

    expect(player.currentTime).toBe(23);
    expect(play).toHaveBeenCalledTimes(2);
    expect(api.createDocumentAccessUrl).toHaveBeenCalledTimes(2);
  });

  it('does not loop access renewal when metadata loads but the codec then fails', async () => {
    const refreshed = { ...access, url: 'https://storage.example/media?codec-retry' };
    vi.spyOn(api, 'createDocumentAccessUrl').mockResolvedValue(refreshed);
    render(
      <MediaTranscriptPlayer
        documentId="doc-1"
        filename="meeting.mp3"
        kind="audio"
        initialAccess={access}
      />,
    );
    const player = screen.getByLabelText('Audio player for meeting.mp3') as HTMLAudioElement;

    fireEvent.error(player);
    await waitFor(() => expect(player).toHaveAttribute('src', refreshed.url));
    Object.defineProperty(player, 'readyState', {
      configurable: true,
      value: HTMLMediaElement.HAVE_METADATA,
    });
    fireEvent.loadedMetadata(player);
    fireEvent.error(player);

    expect(await screen.findByRole('alert')).toHaveTextContent(/codec.*cannot play/i);
    expect(api.createDocumentAccessUrl).toHaveBeenCalledTimes(1);
  });
});
