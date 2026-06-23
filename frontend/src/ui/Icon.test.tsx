import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { Icon } from './Icon';

describe('Icon', () => {
  it('renders an aria-hidden decorative glyph by default', () => {
    const { container } = render(<Icon name="check" />);
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveClass('lc-icon');
    expect(svg?.querySelector('path')).toHaveAttribute('d');
  });

  it('becomes a labelled image when a title is supplied', () => {
    const { container, getByText } = render(<Icon name="shield-check" title="Access granted" />);
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('role', 'img');
    expect(svg).not.toHaveAttribute('aria-hidden');
    expect(getByText('Access granted').tagName.toLowerCase()).toBe('title');
  });

  it('merges extra class names', () => {
    const { container } = render(<Icon name="x" className="lc-trace__chevron" />);
    expect(container.querySelector('svg')).toHaveClass('lc-icon', 'lc-trace__chevron');
  });
});
