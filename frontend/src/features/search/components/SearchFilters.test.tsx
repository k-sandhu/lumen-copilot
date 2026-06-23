/**
 * SearchFilters (#118) — the filter sidebar reflects ONLY real, contract-backed
 * scopes: collections (`collection_id`), the frozen `ResultSource` enum
 * (uploaded docs / chat messages / connected sources), and a content-type facet
 * DERIVED from the data. It never renders an invented connector (Slack/Jira/etc.),
 * and toggling a facet calls back with the right scope.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Collection, SearchResult } from '@/api';
import { SearchFilters, type SearchFilterState } from './SearchFilters';

const collection: Collection = {
  id: 'col-1',
  name: 'Finance',
  owner_id: 'u1',
  document_count: 4,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function result(source: SearchResult['source'], type: string, id = `${source}-${type}`): SearchResult {
  return {
    id,
    title: 't',
    snippet: 's',
    match_spans: [],
    why_matched: 'w',
    source,
    type,
    last_indexed: '2026-06-01T00:00:00Z',
    permission: 'allowed',
  };
}

function renderFilters(state: SearchFilterState = {}, results: SearchResult[] = []) {
  const onChange = vi.fn();
  render(
    <SearchFilters
      state={state}
      onChange={onChange}
      collections={[collection]}
      results={results}
    />,
  );
  return { onChange };
}

describe('SearchFilters', () => {
  it('renders only the real source kinds present in the data — no invented connectors', () => {
    renderFilters({}, [result('upload', 'document'), result('chat', 'message')]);
    const nav = screen.getByRole('navigation', { name: /search filters/i });
    expect(within(nav).getByText('Uploaded documents')).toBeInTheDocument();
    expect(within(nav).getByText('Chat messages')).toBeInTheDocument();
    // The wireframe's fictional connectors must never appear.
    expect(
      within(nav).queryByText(/slack|jira|tickets|github|salesforce|confluence|people|code/i),
    ).toBeNull();
  });

  it('lists the caller’s collections with their document counts', () => {
    renderFilters({}, [result('upload', 'document')]);
    const nav = screen.getByRole('navigation', { name: /search filters/i });
    const row = within(nav).getByRole('checkbox', { name: /finance/i });
    expect(row).toHaveTextContent('Finance');
    expect(row).toHaveTextContent('4');
  });

  it('toggles a collection scope via the callback', async () => {
    const { onChange } = renderFilters({}, [result('upload', 'document')]);
    await userEvent.setup().click(screen.getByRole('checkbox', { name: /finance/i }));
    expect(onChange).toHaveBeenCalledWith({ collectionId: 'col-1' });
  });

  it('toggles a source scope to the frozen enum value', async () => {
    const { onChange } = renderFilters({}, [result('chat', 'message')]);
    await userEvent.setup().click(screen.getByRole('checkbox', { name: /chat messages/i }));
    expect(onChange).toHaveBeenCalledWith({ source: 'chat' });
  });

  it('derives the content-type facet from the data', () => {
    renderFilters({}, [result('upload', 'document'), result('chat', 'message')]);
    const nav = screen.getByRole('navigation', { name: /search filters/i });
    expect(within(nav).getByRole('checkbox', { name: /^document/i })).toBeInTheDocument();
    expect(within(nav).getByRole('checkbox', { name: /^message/i })).toBeInTheDocument();
  });

  it('marks the active facet checked (aria-checked)', () => {
    renderFilters({ source: 'upload' }, [result('upload', 'document')]);
    expect(
      screen.getByRole('checkbox', { name: /uploaded documents/i }),
    ).toHaveAttribute('aria-checked', 'true');
  });
});
