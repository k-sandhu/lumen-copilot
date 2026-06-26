import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { NotFoundPage } from './NotFoundPage';

describe('NotFoundPage', () => {
  it('renders an accessible in-shell 404 with a route back to the assistant', () => {
    render(
      <MemoryRouter initialEntries={['/missing-page']}>
        <NotFoundPage />
      </MemoryRouter>,
    );

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('Page not found')).toBeInTheDocument();
    expect(within(alert).getByText('/missing-page')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to assistant/i })).toHaveAttribute('href', '/');
  });
});
