/**
 * DocsPage state coverage: success (nav + rendered doc), not-found (unknown slug
 * → actionable alert), and the index redirect to a real default doc. Routed via
 * the same `/docs/*` splat the app uses.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { DocsPage } from './DocsPage';
import { DOCS } from '../content';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/docs/*" element={<DocsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('DocsPage', () => {
  it('renders the nav and the selected document', async () => {
    const adr = DOCS.find((d) => d.slug === 'architecture/0003-application-stack');
    if (!adr) throw new Error('fixture doc missing — bundling changed?');

    renderAt(`/docs/${adr.slug}`);

    expect(await screen.findByRole('navigation', { name: /documentation/i })).toBeInTheDocument();
    // The doc's H1 renders as a heading (the nav shows the same text as a link).
    expect(screen.getByRole('heading', { name: adr.title })).toBeInTheDocument();
  });

  it('shows an actionable not-found state for an unknown slug', async () => {
    renderAt('/docs/does/not-exist');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/document not found/i);
    expect(screen.getByRole('link', { name: /back to documentation/i })).toBeInTheDocument();
  });

  it('redirects the bare /docs index to a real default doc', async () => {
    renderAt('/docs');

    expect(await screen.findByRole('navigation', { name: /documentation/i })).toBeInTheDocument();
    expect(screen.queryByText(/document not found/i)).not.toBeInTheDocument();
  });
});
