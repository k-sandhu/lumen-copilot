/**
 * DocumentPreviewBody (#242/#245) — the shared preview body. Covers the type
 * branches (blob-iframe vs extracted text), the truncation notice, the INV-2
 * 404 (no retry), the actionable error path, download-original, and object-URL
 * revocation on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DocumentPreviewBody } from './DocumentPreviewBody';
import { ApiError } from '@/api';

const fetchDocumentContent = vi.hoisted(() => vi.fn());
const fetchDocumentText = vi.hoisted(() => vi.fn());

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, fetchDocumentContent, fetchDocumentText };
});

const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

beforeEach(() => {
  fetchDocumentContent.mockReset();
  fetchDocumentText.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe('DocumentPreviewBody', () => {
  it('renders a PDF in an UNsandboxed iframe so Chrome’s native viewer can load', async () => {
    const revoke = vi.fn();
    fetchDocumentContent.mockResolvedValue({
      url: 'blob:pdf-1',
      type: 'application/pdf',
      revoke,
    });

    const view = render(
      <DocumentPreviewBody documentId="doc-1" filename="msa.pdf" mimeType="application/pdf" />,
    );

    const frame = await screen.findByTitle(/preview of msa\.pdf/i);
    expect(frame).toHaveAttribute('src', 'blob:pdf-1');
    // A fully-restrictive sandbox="" blocks Chrome's out-of-process PDF viewer,
    // so a PDF frame must carry NO sandbox attribute at all.
    expect(frame).not.toHaveAttribute('sandbox');
    expect(fetchDocumentText).not.toHaveBeenCalled();

    view.unmount();
    expect(revoke).toHaveBeenCalledTimes(1); // no object-URL leaks
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
    expect(fetchDocumentContent).not.toHaveBeenCalled();
  });

  it('renders plain text via /text once the blob type says text (chat citation, no mime)', async () => {
    const revoke = vi.fn();
    fetchDocumentContent.mockResolvedValue({ url: 'blob:txt-1', type: 'text/plain', revoke });
    fetchDocumentText.mockResolvedValue({ text: 'runbook body', chunk_count: 1, truncated: false });

    render(<DocumentPreviewBody documentId="doc-txt" filename="runbook.txt" />);

    expect(await screen.findByText('runbook body')).toBeInTheDocument();
    expect(revoke).toHaveBeenCalledTimes(1); // bytes released; text served via /text
  });

  it('renders a PDF chat citation (no mime, blob type pdf) in the unsandboxed iframe', async () => {
    fetchDocumentContent.mockResolvedValue({
      url: 'blob:pdf-x',
      type: 'application/pdf',
      revoke: vi.fn(),
    });

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
    expect(fetchDocumentContent).not.toHaveBeenCalled();
  });

  it('falls back to the blob content-type when no mime type is supplied (chat citations)', async () => {
    const revoke = vi.fn();
    fetchDocumentContent.mockResolvedValue({ url: 'blob:docx-1', type: DOCX, revoke });
    fetchDocumentText.mockResolvedValue({
      text: 'from the wire',
      chunk_count: 1,
      truncated: false,
    });

    render(<DocumentPreviewBody documentId="doc-3" filename="plan.docx" />);

    expect(await screen.findByText('from the wire')).toBeInTheDocument();
    // Office bytes are not rendered; their blob URL is released immediately.
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it('shows the truncation notice when the server capped the text', async () => {
    fetchDocumentText.mockResolvedValue({ text: 'partial…', chunk_count: 900, truncated: true });

    render(<DocumentPreviewBody documentId="doc-4" filename="big.docx" mimeType={DOCX} />);

    expect(await screen.findByText(/preview truncated/i)).toBeInTheDocument();
  });

  it('renders "no longer available" with no retry on 404 (INV-2)', async () => {
    fetchDocumentContent.mockRejectedValue(new ApiError('Content request failed with 404', 404));

    render(
      <DocumentPreviewBody documentId="doc-5" filename="gone.pdf" mimeType="application/pdf" />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no longer available/i);
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('keeps other failures actionable: Retry refetches', async () => {
    const revoke = vi.fn();
    fetchDocumentContent
      .mockRejectedValueOnce(new ApiError('Network request failed', 0))
      .mockResolvedValueOnce({ url: 'blob:pdf-2', type: 'application/pdf', revoke });
    const user = userEvent.setup();

    render(
      <DocumentPreviewBody documentId="doc-6" filename="msa.pdf" mimeType="application/pdf" />,
    );

    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: /retry/i }));

    const frame = await screen.findByTitle(/preview of msa\.pdf/i);
    expect(frame).toHaveAttribute('src', 'blob:pdf-2');
  });

  it('offers Download original when office text extraction fails (no dead end)', async () => {
    fetchDocumentText.mockRejectedValue(new ApiError('boom', 500));

    render(<DocumentPreviewBody documentId="doc-7" filename="plan.docx" mimeType={DOCX} />);

    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: /download original/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('downloads the original on demand and revokes the blob URL after the click', async () => {
    fetchDocumentText.mockResolvedValue({ text: 'body', chunk_count: 1, truncated: false });
    const revoke = vi.fn();
    fetchDocumentContent.mockResolvedValue({ url: 'blob:dl-1', type: DOCX, revoke });
    const user = userEvent.setup();

    render(<DocumentPreviewBody documentId="doc-8" filename="plan.docx" mimeType={DOCX} />);
    await screen.findByText('body');

    await user.click(screen.getByRole('button', { name: /download original/i }));

    await waitFor(() => expect(fetchDocumentContent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(revoke).toHaveBeenCalledTimes(1));
  });
});
