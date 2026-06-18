/**
 * Documents feature root (#49) — composes the collections sidebar, the upload
 * zone, the per-collection document list, and the document viewer into a
 * two-pane, independently-scrollable layout (frontend/AGENTS.md). Each pane has
 * its own ErrorBoundary so one failing region never wedges the others.
 *
 * Local UI state (selected collection, the document open in the viewer) lives
 * here; everything else is server state via the queries hooks.
 */
import { useState } from 'react';
import type { Document } from '@/api';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ScrollArea } from '@/components/ScrollArea';
import { useCollections } from '../model/queries';
import { CollectionsSidebar } from './CollectionsSidebar';
import { DocumentUpload } from './DocumentUpload';
import { DocumentList } from './DocumentList';
import { DocumentViewer } from './DocumentViewer';

export function DocumentsPanel() {
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const [openDoc, setOpenDoc] = useState<Document | null>(null);

  // Auto-select the first collection once they load (so the right pane isn't
  // empty when collections already exist). Read-only — does not mirror state.
  const collections = useCollections();
  const firstId = collections.data?.items[0]?.id;
  const effectiveId =
    selectedId && collections.data?.items.some((c) => c.id === selectedId)
      ? selectedId
      : firstId;

  return (
    <div className="flex h-full min-h-0">
      {/* Left: collections sidebar — independently scrollable */}
      <aside className="flex w-72 shrink-0 flex-col border-r border-border">
        <ErrorBoundary label="Collections">
          <CollectionsSidebar selectedId={effectiveId} onSelect={setSelectedId} />
        </ErrorBoundary>
      </aside>

      {/* Right: upload + document list */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-border p-4">
          <ErrorBoundary label="Upload">
            <DocumentUpload collectionId={effectiveId} />
          </ErrorBoundary>
        </div>
        <div className="min-h-0 flex-1">
          {effectiveId ? (
            <ErrorBoundary label="Documents">
              <DocumentList collectionId={effectiveId} onOpen={setOpenDoc} />
            </ErrorBoundary>
          ) : (
            <ScrollArea viewportClassName="p-6">
              <p className="text-sm text-foreground-muted">
                Create a collection on the left to start uploading and organizing documents.
              </p>
            </ScrollArea>
          )}
        </div>
      </main>

      {openDoc && (
        <ErrorBoundary label="Document viewer">
          <DocumentViewer doc={openDoc} onClose={() => setOpenDoc(null)} />
        </ErrorBoundary>
      )}
    </div>
  );
}
