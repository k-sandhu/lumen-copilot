/**
 * Documents feature root (#49) — composes the collections sidebar, the upload
 * zone, the per-collection document list, and the document viewer into a
 * two-pane, independently-scrollable layout (frontend/AGENTS.md). Each pane has
 * its own ErrorBoundary so one failing region never wedges the others.
 *
 * Local UI state (selected collection, the document open in the viewer) lives
 * here; everything else is server state via the queries hooks.
 */
import { useId, useRef, useState } from 'react';
import type { Document } from '@/api';
import { useCurrentUser } from '@/features/auth';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { cn } from '@/lib/cn';
import { ScrollArea } from '@/components/ScrollArea';
import { useCollections } from '../model/queries';
import { CollectionsSidebar } from './CollectionsSidebar';
import { DocumentUpload } from './DocumentUpload';
import { DocumentList } from './DocumentList';
import { DocumentViewer } from './DocumentViewer';

export function DocumentsPanel() {
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const [openDoc, setOpenDoc] = useState<Document | null>(null);
  const [collectionsOpen, setCollectionsOpen] = useState(false);
  const collectionPanelId = useId();
  const collectionTrigger = useRef<HTMLButtonElement>(null);

  const closeCollections = () => {
    setCollectionsOpen(false);
    collectionTrigger.current?.focus();
  };

  // Auto-select the first collection once they load (so the right pane isn't
  // empty when collections already exist). Read-only — does not mirror state.
  const collections = useCollections();
  const firstId = collections.data?.items[0]?.id;
  const effectiveId =
    selectedId && collections.data?.items.some((c) => c.id === selectedId) ? selectedId : firstId;
  const selectedCollection = collections.data?.items.find((c) => c.id === effectiveId);

  // Identify the signed-in user so the Owner column can read "You" for their own
  // documents (the contract carries owner_id, not a display name).
  const me = useCurrentUser();
  const currentUserId = me.data?.id;

  return (
    <div
      className="flex h-full min-h-0 min-w-0 flex-col md:flex-row"
      onKeyDown={(event) => {
        if (collectionsOpen && event.key === 'Escape') {
          event.preventDefault();
          closeCollections();
        }
      }}
    >
      <div className="shrink-0 border-b border-border p-3 md:hidden">
        <button
          ref={collectionTrigger}
          type="button"
          aria-expanded={collectionsOpen}
          aria-controls={collectionPanelId}
          onClick={() => setCollectionsOpen((open) => !open)}
          className="flex w-full min-w-0 items-center justify-between gap-2 rounded-md border border-border bg-surface px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span className="min-w-0 truncate">
            <span className="font-medium">Choose collection</span>
            {selectedCollection ? `: ${selectedCollection.name}` : ''}
          </span>
          <span aria-hidden="true" className="shrink-0">
            {collectionsOpen ? '−' : '+'}
          </span>
        </button>
      </div>
      {/* Left: collections sidebar — independently scrollable */}
      <aside
        id={collectionPanelId}
        aria-label="Document collections"
        className={cn(
          'h-64 max-h-[50%] w-full shrink-0 flex-col border-b border-border md:flex md:h-full md:max-h-none md:w-72 md:border-b-0 md:border-r',
          collectionsOpen ? 'flex' : 'hidden',
        )}
      >
        <ErrorBoundary label="Collections">
          <CollectionsSidebar
            selectedId={effectiveId}
            onSelect={(id) => {
              setSelectedId(id);
              if (collectionsOpen) closeCollections();
            }}
          />
        </ErrorBoundary>
      </aside>

      {/* Right: page heading + upload + document list */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-border p-4">
          <header className="mb-3">
            <h1 className="text-lg font-semibold text-foreground">Documents</h1>
            <p className="mt-0.5 text-sm text-foreground-muted">
              Upload many file types — each is parsed, chunked, embedded, and permission-scoped.
            </p>
          </header>
          <ErrorBoundary label="Upload">
            <DocumentUpload collectionId={effectiveId} />
          </ErrorBoundary>
        </div>
        <div className="min-h-0 flex-1">
          {effectiveId ? (
            <ErrorBoundary label="Documents">
              <DocumentList
                collectionId={effectiveId}
                collectionName={selectedCollection?.name}
                currentUserId={currentUserId}
                onOpen={setOpenDoc}
              />
            </ErrorBoundary>
          ) : (
            <ScrollArea viewportClassName="p-6">
              <p className="text-sm text-foreground-muted">
                Create a collection in Collections to start uploading and organizing documents.
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
