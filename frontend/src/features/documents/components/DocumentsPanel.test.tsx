import { afterEach, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { DocumentsPanel } from './DocumentsPanel';

vi.mock('@/features/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/auth')>()),
  useCurrentUser: () => ({ data: { id: 'u1' } }),
}));
vi.mock('./DocumentList', () => ({
  DocumentList: ({ collectionId }: { collectionId: string }) => (
    <div>Document list: {collectionId}</div>
  ),
}));
vi.mock('./DocumentUpload', () => ({
  DocumentUpload: ({ collectionId }: { collectionId?: string }) => (
    <div>Upload target: {collectionId ?? 'none'}</div>
  ),
}));

function json(items: unknown[]) {
  return new Response(JSON.stringify({ items, next_cursor: null }), {
    headers: { 'Content-Type': 'application/json' },
  });
}
function mockCollections() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
    json([
      { id: 'c1', name: 'Policies', document_count: 1 },
      { id: 'c2', name: 'Other', document_count: 2 },
    ]),
  );
}
afterEach(() => vi.restoreAllMocks());

it('offers a labelled disclosure and preserves selection/upload target with focus return', async () => {
  mockCollections();
  renderWithQuery(<DocumentsPanel />);
  await screen.findByText('Document list: c1');
  const user = userEvent.setup();
  const trigger = screen.getByRole('button', { name: /Choose collection/ });
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(trigger).toHaveTextContent('Policies');
  const panel = document.getElementById(trigger.getAttribute('aria-controls')!);
  expect(panel).toHaveClass('hidden', 'md:flex');
  await user.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await user.click(screen.getByRole('button', { name: /^Other/ }));
  expect(trigger).toHaveFocus();
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(trigger).toHaveTextContent('Other');
  expect(screen.getByText('Document list: c2')).toBeInTheDocument();
  expect(screen.getByText('Upload target: c2')).toBeInTheDocument();
  await user.click(trigger);
  await user.keyboard('{Escape}');
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(trigger).toHaveFocus();
});

it('keeps empty and failed collection navigation reachable through the disclosure', async () => {
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ title: 'Unavailable', status: 503 }), {
        status: 503,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    )
    .mockImplementation(async () => json([]));
  renderWithQuery(<DocumentsPanel />);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /Choose collection/ }));
  const retry = await screen.findByRole('button', { name: 'Retry' });
  await user.click(retry);
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument(),
  );
  expect(screen.getByText(/no collections yet/i)).toBeInTheDocument();
  expect(screen.getByText(/create a collection in Collections/i)).toBeInTheDocument();
  expect(screen.getByText('Upload target: none')).toBeInTheDocument();
});
