/**
 * DocumentPreviewBody (#242/#245) — the shared preview body. Covers the type
 * branches (signed PDF/media vs extracted text), the truncation notice, INV-2
 * 404 (no retry), actionable errors, and purpose-bound original downloads.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DocumentPreviewBody } from './DocumentPreviewBody';
import { ApiError } from '@/api';

const createDocumentAccessUrl = vi.hoisted(() => vi.fn());
const fetchDocumentText = vi.hoisted(() => vi.fn());
const fetchDocumentTranscript = vi.hoisted(() => vi.fn());

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, createDocumentAccessUrl, fetchDocumentText, fetchDocumentTranscript };
});

const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

beforeEach(() => {
  createDocumentAccessUrl.mockReset();
  fetchDocumentText.mockReset();
  fetchDocumentTranscript.mockReset();
  fetchDocumentTranscript.mockResolvedValue({
    document_id: 'doc-media',
    duration_ms: 60_000,
    language: 'en',
    transcription_model: 'x-ai/grok-stt-1.0',
    speakers: [],
    items: [],
    next_cursor: null,
  });
});
afterEach(() => vi.restoreAllMocks());

describe('DocumentPreviewBody', () => {
  it('renders a PDF in an UNsandboxed iframe so Chrome’s native viewer can load', async () => {
    createDocumentAccessUrl.mockResolvedValue(
      access('https://storage.test/pdf-1', 'application/pdf'),
    );

    const view = render(
      <DocumentPreviewBody documentId="doc-1" filename="msa.pdf" mimeType="application/pdf" />,
    );

    const frame = await screen.findByTitle(/preview of msa\.pdf/i);
    expect(frame).toHaveAttribute('src', 'https://storage.test/pdf-1');
    // A fully-restrictive sandbox="" blocks Chrome's out-of-process PDF viewer,
    // so a PDF frame must carry NO sandbox attribute at all.
    expect(frame).not.toHaveAttribute('sandbox');
    expect(fetchDocumentText).not.toHaveBeenCalled();

    view.unmount();
  });

  it('renders markdown as server-extracted text — not a blob iframe', async () => {
    // A blob iframe renders text/* blank under a restrictive sandbox, so text
    // types go through GET /documents/{id}/text just like office types.
    fetchDocumentText.mockResolvedValue({
      text: '# PRD\n\nBody',
      chunk_count: 2,
      truncated: false,
    });

    render(
      <DocumentPreviewBody documentId="doc-md" filename="notes.md" mimeType="text/markdown" />,
    );

    expect(await screen.findByText(/# PRD/)).toBeInTheDocument();
    expect(screen.getByText(/extracted text preview/i)).toBeInTheDocument();
    expect(screen.queryByTitle(/preview of notes\.md/i)).not.toBeInTheDocument(); // no iframe
    expect(createDocumentAccessUrl).not.toHaveBeenCalled();
  });

  it('renders plain text via /text once the blob type says text (chat citation, no mime)', async () => {
    createDocumentAccessUrl.mockResolvedValue(access('https://storage.test/txt-1', 'text/plain'));
    fetchDocumentText.mockResolvedValue({ text: 'runbook body', chunk_count: 1, truncated: false });

    render(<DocumentPreviewBody documentId="doc-txt" filename="runbook.txt" />);

    expect(await screen.findByText('runbook body')).toBeInTheDocument();
    expect(createDocumentAccessUrl).toHaveBeenCalledWith(
      'doc-txt',
      'preview',
      expect.any(AbortSignal),
    );
  });

  it('renders a PDF chat citation (no mime, blob type pdf) in the unsandboxed iframe', async () => {
    createDocumentAccessUrl.mockResolvedValue(
      access('https://storage.test/pdf-x', 'application/pdf'),
    );

    render(<DocumentPreviewBody documentId="doc-x" filename="Q4 strategy.pdf" />);

    const frame = await screen.findByTitle(/preview of q4 strategy\.pdf/i);
    expect(frame).not.toHaveAttribute('sandbox');
    expect(fetchDocumentText).not.toHaveBeenCalled();
  });

  it('renders extracted text for a known office type without fetching bytes', async () => {
    fetchDocumentText.mockResolvedValue({
      text: 'quarterly numbers',
      chunk_count: 3,
      truncated: false,
    });

    render(<DocumentPreviewBody documentId="doc-2" filename="plan.docx" mimeType={DOCX} />);

    expect(await screen.findByText('quarterly numbers')).toBeInTheDocument();
    expect(screen.getByText(/extracted text preview/i)).toBeInTheDocument();
    expect(createDocumentAccessUrl).not.toHaveBeenCalled();
  });

  it('falls back to the blob content-type when no mime type is supplied (chat citations)', async () => {
    createDocumentAccessUrl.mockResolvedValue(access('https://storage.test/docx-1', DOCX));
    fetchDocumentText.mockResolvedValue({
      text: 'from the wire',
      chunk_count: 1,
      truncated: false,
    });

    render(<DocumentPreviewBody documentId="doc-3" filename="plan.docx" />);

    expect(await screen.findByText('from the wire')).toBeInTheDocument();
    expect(createDocumentAccessUrl).toHaveBeenCalledTimes(1);
  });

  it('shows the truncation notice when the server capped the text', async () => {
    fetchDocumentText.mockResolvedValue({ text: 'partial…', chunk_count: 900, truncated: true });

    render(<DocumentPreviewBody documentId="doc-4" filename="big.docx" mimeType={DOCX} />);

    expect(await screen.findByText(/preview truncated/i)).toBeInTheDocument();
  });

  it('renders "no longer available" with no retry on 404 (INV-2)', async () => {
    createDocumentAccessUrl.mockRejectedValue(new ApiError('Content request failed with 404', 404));

    render(
      <DocumentPreviewBody documentId="doc-5" filename="gone.pdf" mimeType="application/pdf" />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no longer available/i);
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('keeps other failures actionable: Retry refetches', async () => {
    createDocumentAccessUrl
      .mockRejectedValueOnce(new ApiError('Network request failed', 0))
      .mockResolvedValueOnce(access('https://storage.test/pdf-2', 'application/pdf'));
    const user = userEvent.setup();

    render(
      <DocumentPreviewBody documentId="doc-6" filename="msa.pdf" mimeType="application/pdf" />,
    );

    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: /retry/i }));

    const frame = await screen.findByTitle(/preview of msa\.pdf/i);
    expect(frame).toHaveAttribute('src', 'https://storage.test/pdf-2');
  });

  it('offers Download original when office text extraction fails (no dead end)', async () => {
    fetchDocumentText.mockRejectedValue(new ApiError('boom', 500));

    render(<DocumentPreviewBody documentId="doc-7" filename="plan.docx" mimeType={DOCX} />);

    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: /download original/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('downloads the original through a purpose-bound signed access URL', async () => {
    fetchDocumentText.mockResolvedValue({ text: 'body', chunk_count: 1, truncated: false });
    createDocumentAccessUrl.mockResolvedValue(
      access('https://storage.test/download', DOCX, 'download'),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);
    const user = userEvent.setup();

    render(<DocumentPreviewBody documentId="doc-8" filename="plan.docx" mimeType={DOCX} />);
    await screen.findByText('body');

    await user.click(screen.getByRole('button', { name: /download original/i }));

    await waitFor(() => expect(createDocumentAccessUrl).toHaveBeenCalledWith('doc-8', 'download'));
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('keeps a purpose-bound original download beside media playback', async () => {
    createDocumentAccessUrl.mockImplementation(
      async (_documentId: string, purpose: 'preview' | 'download') =>
        access(
          purpose === 'preview'
            ? 'https://storage.test/audio-preview'
            : 'https://storage.test/audio-download',
          'audio/mpeg',
          purpose,
        ),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);
    const user = userEvent.setup();

    render(<DocumentPreviewBody documentId="doc-media" filename="meeting.mp3" />);

    const player = await screen.findByLabelText('Audio player for meeting.mp3');
    expect(player).toHaveAttribute('src', 'https://storage.test/audio-preview');
    await user.click(screen.getByRole('button', { name: /download original/i }));

    await waitFor(() =>
      expect(createDocumentAccessUrl).toHaveBeenCalledWith('doc-media', 'download'),
    );
    expect(createDocumentAccessUrl).toHaveBeenNthCalledWith(
      1,
      'doc-media',
      'preview',
      expect.any(AbortSignal),
    );
    expect(click).toHaveBeenCalledTimes(1);
  });
});

function access(url: string, mimeType: string, purpose: 'preview' | 'download' = 'preview') {
  return {
    url,
    filename: 'file',
    mime_type: mimeType,
    size_bytes: 10,
    expires_at: '2030-01-01T00:00:00Z',
    purpose,
    supports_byte_ranges: true,
  };
}
