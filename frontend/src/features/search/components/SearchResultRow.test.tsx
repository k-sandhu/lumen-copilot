/**
 * SearchResultRow (#84): a ranked result row carries the trust signals — the
 * matched snippet is <mark>-highlighted from match_spans, the why-it-matched
 * rationale, owner, freshness, and a permission pill all render. A `restricted`
 * result shows the restricted pill (content withheld) rather than the granted one.
 * With an `onOpen` handler (#375) the title is an "Open …" button; without one
 * the row stays non-interactive.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SearchResult } from '@/api';
import { SearchResultRow } from './SearchResultRow';

const base: SearchResult = {
  id: 'r1',
  title: 'PTO Policy 2026',
  snippet: 'Employees accrue 20 days of paid time off per year.',
  match_spans: [{ start: 0, end: 9 }], // "Employees"
  why_matched: 'semantic + title',
  source: 'upload',
  type: 'document',
  owner: 'Dana Ruiz',
  last_indexed: new Date().toISOString(),
  permission: 'allowed',
};

describe('SearchResultRow', () => {
  it('renders the title, type, owner and why-it-matched rationale', () => {
    render(<SearchResultRow result={base} />);
    const row = screen.getByRole('article', { name: /PTO Policy 2026/i });
    expect(within(row).getByRole('heading', { name: 'PTO Policy 2026' })).toBeInTheDocument();
    expect(within(row).getByText('document')).toBeInTheDocument();
    expect(within(row).getByText('Dana Ruiz')).toBeInTheDocument();
    expect(within(row).getByText(/why it matched/i)).toBeInTheDocument();
    expect(within(row).getByText(/semantic \+ title/i)).toBeInTheDocument();
  });

  it('highlights the matched span with <mark>', () => {
    const { container } = render(<SearchResultRow result={base} />);
    const marks = container.querySelectorAll('mark');
    expect(marks).toHaveLength(1);
    expect(marks[0]?.textContent).toBe('Employees');
  });

  it('shows a granted permission pill for an allowed result', () => {
    render(<SearchResultRow result={base} />);
    expect(screen.getByText(/you have access/i)).toBeInTheDocument();
  });

  it('shows a restricted permission pill (content withheld) for a restricted result', () => {
    render(<SearchResultRow result={{ ...base, permission: 'restricted' }} />);
    expect(screen.getByText(/withheld/i)).toBeInTheDocument();
  });

  it('marks a stale result as past its freshness window', () => {
    const old = new Date(Date.now() - 200 * 24 * 3600_000).toISOString();
    const { container } = render(<SearchResultRow result={{ ...base, last_indexed: old }} />);
    expect(container.querySelector('.lc-fresh--stale')).toBeTruthy();
  });

  it('omits the owner line when the result has no owner', () => {
    render(<SearchResultRow result={{ ...base, owner: null }} />);
    expect(screen.queryByText('Dana Ruiz')).not.toBeInTheDocument();
  });

  it('renders the title as an "Open …" button and fires onOpen when clicked (#375)', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<SearchResultRow result={base} onOpen={onOpen} />);
    const button = screen.getByRole('button', { name: 'Open PTO Policy 2026' });
    await user.click(button);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('stays non-interactive (plain heading, no button) without onOpen', () => {
    render(<SearchResultRow result={base} />);
    expect(screen.getByRole('heading', { name: 'PTO Policy 2026' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('keeps the focus ring unclipped: no overflow-hidden ancestor above the Open button', () => {
    // The focus ring draws OUTSIDE the button's border box; any ancestor with
    // `truncate`/`overflow-hidden` between the button and the card would clip it
    // (visible-focus bar, frontend/AGENTS.md). Ellipsis belongs to the inner span.
    render(<SearchResultRow result={base} onOpen={() => {}} />);
    const button = screen.getByRole('button', { name: 'Open PTO Policy 2026' });
    for (let el = button.parentElement; el && el.tagName !== 'ARTICLE'; el = el.parentElement) {
      expect(el.className).not.toMatch(/truncate|overflow-hidden/);
    }
    expect(button.className).not.toMatch(/truncate|overflow-hidden/);
  });
});
