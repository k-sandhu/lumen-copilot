/**
 * TrimNotice (#84): discloses the permission trim from hidden_count without
 * leaking content (spec 0004 INV-2); renders nothing when nothing was hidden.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TrimNotice } from './TrimNotice';

describe('TrimNotice', () => {
  it('discloses the hidden count', () => {
    render(<TrimNotice hiddenCount={3} />);
    expect(screen.getByRole('note', { name: /permission trim/i })).toHaveTextContent(
      "3 results hidden — you don't have access",
    );
  });

  it('uses the singular noun for one hidden result', () => {
    render(<TrimNotice hiddenCount={1} />);
    expect(screen.getByRole('note')).toHaveTextContent(/1 result hidden/);
  });

  it('renders nothing when nothing was hidden', () => {
    const { container } = render(<TrimNotice hiddenCount={0} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});
