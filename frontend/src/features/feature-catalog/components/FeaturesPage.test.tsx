/**
 * FeaturesPage: renders grouped, status-badged cards from the catalog. Routed
 * (uses <Link>), so it renders inside a MemoryRouter.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FeaturesPage } from './FeaturesPage';

function renderPage() {
  return render(
    <MemoryRouter>
      <FeaturesPage />
    </MemoryRouter>,
  );
}

describe('FeaturesPage', () => {
  it('renders the page title and a status legend', () => {
    renderPage();
    expect(screen.getByRole('heading', { name: 'Features built', level: 1 })).toBeInTheDocument();
    // Legend + cards both use these labels, so just assert presence.
    expect(screen.getAllByText('Shipped').length).toBeGreaterThan(0);
    expect(screen.getAllByText('In progress').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Planned').length).toBeGreaterThan(0);
  });

  it('renders grounded feature cards grouped by area with links', () => {
    renderPage();
    // A known shipped capability.
    expect(screen.getByRole('heading', { name: /LLM model gateway/i })).toBeInTheDocument();
    expect(screen.getByText(/object-storage upload sandbox/i)).toBeInTheDocument();
    // Area grouping → <section aria-label> exposes a region.
    expect(screen.getByRole('region', { name: 'Platform' })).toBeInTheDocument();
    // Internal doc link points into the viewer; external PR link opens out.
    expect(screen.getAllByRole('link', { name: /ADR-0003/i })[0]).toHaveAttribute(
      'href',
      '/docs/architecture/0003-application-stack',
    );
  });
});
