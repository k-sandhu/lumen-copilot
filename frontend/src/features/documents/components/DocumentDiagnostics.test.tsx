import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import type { Document } from '@/api';
import { DocumentViewer } from './DocumentViewer';

const doc: Document = {
  id: 'doc-1',
  filename: 'report.pdf',
  mime_type: 'application/pdf',
  size_bytes: 100,
  collection_id: 'col-1',
  owner_id: 'u-1',
  kind: 'document',
  duration_ms: null,
  status: 'processing',
  chunk_count: 0,
  created_at: '2026-09-30T00:00:00Z',
  updated_at: '2026-09-30T00:00:00Z',
};

describe('permissioned extraction diagnostics', () => {
  it('hides inspection when the API supplies no diagnostics', () => {
    renderWithQuery(<DocumentViewer doc={doc} onClose={() => {}} />);
    expect(screen.queryByRole('region', { name: 'Extraction inspection' })).not.toBeInTheDocument();
  });

  it('shows measured page coverage and qualifies unknown table coverage', () => {
    renderWithQuery(
      <DocumentViewer
        doc={{
          ...doc,
          extraction_diagnostics: {
            character_count: 400,
            replacement_characters: 2,
            suspicious_controls: 1,
            source_part_kind: 'page',
            total_parts: 3,
            parts_with_text: 2,
            blank_parts: [2],
            table_probe: 'unavailable',
            warnings: ['blank_native_parts', 'pdf_table_coverage_unknown'],
          },
        }}
        onClose={() => {}}
      />,
    );
    const inspection = screen.getByRole('region', { name: 'Extraction inspection' });
    expect(inspection).toHaveTextContent('2 of 3 pages contain text');
    expect(inspection).toHaveTextContent('Blank pages: 2');
    expect(inspection).toHaveTextContent('Table coverage was not measured');
    expect(inspection).toHaveTextContent('Latest extraction attempt');
    expect(inspection).toHaveTextContent('Replacement characters: 2');
  });

  it('shows zero measured cells without claiming visual completeness', () => {
    renderWithQuery(
      <DocumentViewer
        doc={{
          ...doc,
          extraction_diagnostics: {
            character_count: 0,
            replacement_characters: 0,
            suspicious_controls: 0,
            blank_parts: [],
            table_probe: 'native_tables',
            table_regions: 0,
            table_cells: 0,
            missing_table_cells: 0,
            warnings: [],
          },
        }}
        onClose={() => {}}
      />,
    );
    const inspection = screen.getByRole('region', { name: 'Extraction inspection' });
    expect(inspection).toHaveTextContent('Measured nonempty cells: 0');
    expect(inspection).toHaveTextContent('Cell text absent from extraction: 0');
    expect(inspection).toHaveTextContent('Text presence does not prove correct table structure');
  });
});
