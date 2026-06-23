/**
 * SearchComposer (#84): type → submit. Enter and the button both submit the
 * TRIMMED query; an empty/whitespace query never submits (so the 422-on-empty
 * path is never fired from the composer); busy disables submit.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { SearchComposer } from './SearchComposer';

function Harness({ onSubmit, busy }: { onSubmit: (q: string) => void; busy?: boolean }) {
  const [value, setValue] = useState('');
  return <SearchComposer value={value} onChange={setValue} onSubmit={onSubmit} busy={busy} />;
}

describe('SearchComposer', () => {
  it('submits the trimmed query on Enter', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByRole('searchbox', { name: /search your sources/i }), '  roadmap  ');
    await user.keyboard('{Enter}');
    expect(onSubmit).toHaveBeenCalledWith('roadmap');
  });

  it('submits via the Search button', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByRole('searchbox'), 'q4 plan');
    await user.click(screen.getByRole('button', { name: /^search$/i }));
    expect(onSubmit).toHaveBeenCalledWith('q4 plan');
  });

  it('does NOT submit an empty / whitespace query', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<Harness onSubmit={onSubmit} />);
    const box = screen.getByRole('searchbox');
    await user.click(box);
    await user.keyboard('{Enter}');
    expect(onSubmit).not.toHaveBeenCalled();
    // The submit button is disabled until there's a real query.
    expect(screen.getByRole('button', { name: /search/i })).toBeDisabled();
  });

  it('shows a busy state and disables submit while a search is in flight', () => {
    render(<Harness onSubmit={() => {}} busy />);
    const button = screen.getByRole('button', { name: /searching/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
  });
});
