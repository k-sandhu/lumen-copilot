/**
 * Collections sidebar (#49 AC-1) — list / create / rename / delete via the
 * /collections endpoints. Every async state is implemented (loading / error /
 * empty / success), errors are actionable retries, and destructive actions
 * confirm. Selecting a collection drives the document list to its right.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { cn } from '@/lib/cn';
import {
  useCollections,
  useCreateCollection,
  useDeleteCollection,
  useUpdateCollection,
} from '../model/queries';
import type { Collection } from '@/api';

interface CollectionsSidebarProps {
  selectedId: string | undefined;
  onSelect: (id: string) => void;
}

export function CollectionsSidebar({ selectedId, onSelect }: CollectionsSidebarProps) {
  const query = useCollections();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Collections</h2>
        <p className="mt-0.5 text-xs text-foreground-muted">Folders that group your documents.</p>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-2 py-2">
          <CollectionsBody query={query} selectedId={selectedId} onSelect={onSelect} />
        </ScrollArea>
      </div>

      <div className="shrink-0 border-t border-border p-3">
        <CreateCollectionForm />
      </div>
    </div>
  );
}

type CollectionsQuery = ReturnType<typeof useCollections>;

function CollectionsBody({
  query,
  selectedId,
  onSelect,
}: {
  query: CollectionsQuery;
  selectedId: string | undefined;
  onSelect: (id: string) => void;
}) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-2 px-1">
        <span className="sr-only">Loading collections…</span>
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-9 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
        ))}
      </div>
    );
  }

  if (query.isError) {
    const message =
      query.error instanceof ApiError ? query.error.displayMessage : 'Could not load collections.';
    return (
      <div role="alert" className="space-y-2 px-1 py-2 text-sm">
        <p className="font-medium text-danger">Couldn’t load collections</p>
        <p className="text-foreground-muted">{message}</p>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted disabled:opacity-60"
        >
          {query.isFetching ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    );
  }

  const collections = query.data.items;
  if (collections.length === 0) {
    return (
      <p className="px-2 py-6 text-center text-sm text-foreground-muted">
        No collections yet. Create one below to start uploading documents.
      </p>
    );
  }

  return (
    <ul aria-label="Collections" className="space-y-0.5">
      {collections.map((collection) => (
        <CollectionRow
          key={collection.id}
          collection={collection}
          selected={collection.id === selectedId}
          onSelect={() => onSelect(collection.id)}
        />
      ))}
    </ul>
  );
}

function CollectionRow({
  collection,
  selected,
  onSelect,
}: {
  collection: Collection;
  selected: boolean;
  onSelect: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(collection.name);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const update = useUpdateCollection();
  const remove = useDeleteCollection();

  // Move focus into the rename field when it opens (managed focus, a11y).
  useEffect(() => {
    if (renaming) renameInputRef.current?.focus();
  }, [renaming]);

  if (renaming) {
    return (
      <li>
        <form
          className="flex items-center gap-1 px-1 py-1"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = name.trim();
            if (!trimmed || trimmed === collection.name) {
              setRenaming(false);
              setName(collection.name);
              return;
            }
            update.mutate(
              { id: collection.id, body: { name: trimmed } },
              { onSuccess: () => setRenaming(false) },
            );
          }}
        >
          <input
            ref={renameInputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label={`Rename ${collection.name}`}
            className="min-w-0 flex-1 rounded border border-border bg-surface px-2 py-1 text-sm"
            maxLength={200}
          />
          <button
            type="submit"
            disabled={update.isPending}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-60"
          >
            {update.isPending ? '…' : 'Save'}
          </button>
          <button
            type="button"
            onClick={() => {
              setRenaming(false);
              setName(collection.name);
            }}
            className="rounded px-2 py-1 text-xs text-foreground-muted hover:bg-surface-muted"
          >
            Cancel
          </button>
        </form>
        {update.isError && (
          <p role="alert" className="px-2 pb-1 text-xs text-danger">
            {update.error instanceof ApiError ? update.error.displayMessage : 'Rename failed.'}
          </p>
        )}
      </li>
    );
  }

  return (
    <li className="group">
      <div
        className={cn(
          'flex items-center gap-2 rounded-md px-2 py-1.5',
          selected ? 'bg-accent/15' : 'hover:bg-surface-muted',
        )}
      >
        <button
          type="button"
          onClick={onSelect}
          aria-current={selected ? 'true' : undefined}
          className="flex min-w-0 flex-1 items-center justify-between gap-2 text-left"
        >
          <span className="truncate text-sm">{collection.name}</span>
          <span className="shrink-0 rounded-full bg-surface-muted px-1.5 py-0.5 text-xs text-foreground-muted">
            {collection.document_count}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 focus-within:opacity-100 group-hover:opacity-100">
          <button
            type="button"
            onClick={() => {
              setName(collection.name);
              setRenaming(true);
            }}
            aria-label={`Rename ${collection.name}`}
            className="rounded p-1 text-xs hover:bg-surface"
          >
            ✎
          </button>
          <button
            type="button"
            disabled={remove.isPending}
            onClick={() => {
              if (
                typeof window !== 'undefined' &&
                !window.confirm(`Delete “${collection.name}” and all its documents?`)
              ) {
                return;
              }
              remove.mutate(collection.id);
            }}
            aria-label={`Delete ${collection.name}`}
            className="rounded p-1 text-xs text-danger hover:bg-surface disabled:opacity-60"
          >
            🗑
          </button>
        </div>
      </div>
      {remove.isError && (
        <p role="alert" className="px-2 pb-1 text-xs text-danger">
          {remove.error instanceof ApiError ? remove.error.displayMessage : 'Delete failed.'}
        </p>
      )}
    </li>
  );
}

function CreateCollectionForm() {
  const [name, setName] = useState('');
  const create = useCreateCollection();

  return (
    <form
      className="space-y-1"
      onSubmit={(e) => {
        e.preventDefault();
        const trimmed = name.trim();
        if (!trimmed) return;
        create.mutate({ name: trimmed }, { onSuccess: () => setName('') });
      }}
    >
      <label htmlFor="new-collection" className="sr-only">
        New collection name
      </label>
      <div className="flex items-center gap-1">
        <input
          id="new-collection"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New collection…"
          maxLength={200}
          className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={create.isPending || name.trim().length === 0}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted disabled:opacity-60"
        >
          {create.isPending ? 'Adding…' : 'Add'}
        </button>
      </div>
      {create.isError && (
        <p role="alert" className="text-xs text-danger">
          {create.error instanceof ApiError ? create.error.displayMessage : 'Could not create the collection.'}
        </p>
      )}
    </form>
  );
}
