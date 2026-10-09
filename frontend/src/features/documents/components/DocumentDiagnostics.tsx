import type { ExtractionDiagnostics } from '@/api';

export function DocumentDiagnostics({ diagnostics: d }: { diagnostics: ExtractionDiagnostics }) {
  const parts = d.source_part_kind ? `${d.source_part_kind}s` : 'parts';
  return (
    <section aria-label="Extraction inspection" className="border-b border-border p-4 text-sm">
      <h3 className="mb-2 font-medium">Extraction inspection</h3>
      <p className="mb-2 text-xs text-foreground-muted">
        Latest extraction attempt. These measurements do not indicate that replacement chunks are
        searchable.
      </p>
      <ul className="space-y-1">
        <li>Extracted characters: {d.character_count}</li>
        <li>Replacement characters: {d.replacement_characters}</li>
        <li>Suspicious controls: {d.suspicious_controls}</li>
        {d.total_parts != null && d.parts_with_text != null ? (
          <li>
            {d.parts_with_text} of {d.total_parts} {parts} contain text
          </li>
        ) : (
          <li>Source-part coverage was not measured</li>
        )}
        {d.blank_parts.length > 0 && (
          <li>
            Blank {parts}: {d.blank_parts.join(', ')}
          </li>
        )}
        {d.table_probe === 'unavailable' ? (
          <li>Table coverage was not measured</li>
        ) : (
          <>
            <li>Measured table or sheet regions: {d.table_regions ?? 'unknown'}</li>
            <li>Measured nonempty cells: {d.table_cells ?? 'unknown'}</li>
            <li>Cell text absent from extraction: {d.missing_table_cells ?? 'unknown'}</li>
          </>
        )}
      </ul>
      <p className="mt-2 text-xs text-foreground-muted">
        Text presence does not prove correct table structure. Blank parts may be intentional; native
        text extraction does not establish whether OCR is needed.
      </p>
      {d.warnings.length > 0 && (
        <p className="mt-2 text-xs text-foreground-muted">
          Inspect the original document where coverage is unknown or extracted text is suspicious.
        </p>
      )}
    </section>
  );
}
