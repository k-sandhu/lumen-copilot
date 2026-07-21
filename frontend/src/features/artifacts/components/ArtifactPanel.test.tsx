/**
 * ArtifactPanel state coverage (#222). Covers the required states + flows:
 * loading skeleton → list; empty state; error + retry (AC-3); an artifact
 * previews its metadata and issues a content request on select (AC-1/AC-2);
 * download issues the content request; delete is gated behind ConfirmDialog and
 * fires DELETE (AC-3). The api/ boundary is mocked so no real HTTP runs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { Artifact, ArtifactContent, ArtifactList } from '@/api';

const listArtifacts = vi.fn<() => Promise<ArtifactList>>();
const deleteArtifact = vi.fn<() => Promise<void>>();
const fetchArtifactContent = vi.fn<() => Promise<ArtifactContent>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listArtifacts: () => listArtifacts(),
    deleteArtifact: () => deleteArtifact(),
    fetchArtifactContent: () => fetchArtifactContent(),
  };
});

import { ArtifactPanel } from './ArtifactPanel';

const art = (over: Partial<Artifact> = {}): Artifact => ({
  id: 'art-1',
  filename: 'report.csv',
  mime_type: 'text/csv',
  size_bytes: 42,
  owner_id: 'u-1',
  produced_by: 'tool',
  created_at: '2026-07-02T00:00:00Z',
  session_id: null,
  run_id: null,
  tool_invocation_id: null,
  ...over,
});

function content(): ArtifactContent {
  return { url: 'blob:art', type: 'text/csv', revoke: vi.fn() };
}

beforeEach(() => {
  listArtifacts.mockReset();
  deleteArtifact.mockReset();
  fetchArtifactContent.mockReset();
  fetchArtifactContent.mockResolvedValue(content());
});

describe('ArtifactPanel', () => {
  it('renders a loading skeleton then the artifact list', async () => {
    listArtifacts.mockResolvedValue({ items: [art()], next_cursor: null });
    renderWithQuery(<ArtifactPanel />);
    expect(screen.getByText(/loading artifacts/i)).toBeInTheDocument();
    expect(await screen.findByText('report.csv')).toBeInTheDocument();
  });

  it('renders the labelled empty state when there are no artifacts', async () => {
    listArtifacts.mockResolvedValue({ items: [], next_cursor: null });
    renderWithQuery(<ArtifactPanel />);
    expect(await screen.findByText(/no artifacts yet/i)).toBeInTheDocument();
  });

  it('renders an actionable error with retry (AC-3)', async () => {
    listArtifacts.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<ArtifactPanel />);
    expect(await screen.findByText(/couldn.t load artifacts/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('selects the first artifact and previews it via a content request (AC-1/AC-2)', async () => {
    listArtifacts.mockResolvedValue({ items: [art()], next_cursor: null });
    renderWithQuery(<ArtifactPanel />);
    // Metadata renders in the detail pane.
    expect(await screen.findByText('From Tool')).toBeInTheDocument();
    // The text preview fetched the bytes (AC-2).
    await waitFor(() => expect(fetchArtifactContent).toHaveBeenCalled());
  });

  it('download issues the content request (AC-1)', async () => {
    listArtifacts.mockResolvedValue({
      items: [art({ mime_type: 'application/octet-stream' })],
      next_cursor: null,
    });
    // An octet-stream is download-only (no preview fetch), so the only content
    // request comes from the Download button — a clean assertion.
    renderWithQuery(<ArtifactPanel />);
    const download = await screen.findByRole('button', { name: /^download$/i });
    await userEvent.click(download);
    await waitFor(() => expect(fetchArtifactContent).toHaveBeenCalled());
  });

  it('delete is gated behind a confirm dialog and fires DELETE (AC-3)', async () => {
    listArtifacts.mockResolvedValue({ items: [art()], next_cursor: null });
    deleteArtifact.mockResolvedValue(undefined);
    renderWithQuery(<ArtifactPanel />);
    const del = await screen.findByRole('button', { name: /delete report\.csv/i });
    await userEvent.click(del);
    // The confirm dialog appears; DELETE has NOT fired yet.
    const dialog = await screen.findByRole('alertdialog');
    expect(deleteArtifact).not.toHaveBeenCalled();
    // Confirm.
    await userEvent.click(within(dialog).getByRole('button', { name: /^delete$/i }));
    await waitFor(() => expect(deleteArtifact).toHaveBeenCalled());
  });

  it('does not delete when the confirm is cancelled (AC-3 negative)', async () => {
    listArtifacts.mockResolvedValue({ items: [art()], next_cursor: null });
    renderWithQuery(<ArtifactPanel />);
    const del = await screen.findByRole('button', { name: /delete report\.csv/i });
    await userEvent.click(del);
    const dialog = await screen.findByRole('alertdialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(deleteArtifact).not.toHaveBeenCalled();
  });

  it('surfaces a failed download inline (AC-3 negative)', async () => {
    listArtifacts.mockResolvedValue({
      items: [art({ mime_type: 'application/octet-stream' })],
      next_cursor: null,
    });
    fetchArtifactContent.mockRejectedValue(new ApiError('nope', 500));
    renderWithQuery(<ArtifactPanel />);
    const download = await screen.findByRole('button', { name: /^download$/i });
    await userEvent.click(download);
    expect(await screen.findByRole('alert')).toHaveTextContent(/download failed|nope/i);
  });
});
