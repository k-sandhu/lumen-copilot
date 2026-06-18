/**
 * Multi-file upload (#49 AC-2/AC-4) — drag-drop zone + file picker. Files upload
 * concurrently into the selected collection via `POST /documents` (multipart);
 * each tile shows live progress and, on completion, success or a clear inline
 * error (413 too large / 415 unsupported / etc. — AC-4). Disabled with guidance
 * until a collection is selected.
 */
import { useRef, useState } from 'react';
import { cn } from '@/lib/cn';
import { useUploadStore } from '../model/uploadStore';
import { useUploadDocuments } from '../model/useUploadDocuments';

interface DocumentUploadProps {
  collectionId: string | undefined;
}

export function DocumentUpload({ collectionId }: DocumentUploadProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const startUploads = useUploadDocuments(collectionId);
  const uploads = useUploadStore((s) => s.uploads);
  const clearFinished = useUploadStore((s) => s.clearFinished);
  const remove = useUploadStore((s) => s.remove);

  const disabled = !collectionId;
  const entries = collectionId
    ? Object.values(uploads).filter((u) => u.collectionId === collectionId)
    : [];
  const hasFinished = entries.some((u) => u.state !== 'uploading');

  function handleFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    startUploads(Array.from(fileList));
  }

  return (
    <section aria-label="Upload documents" className="space-y-3">
      <div
        onDragOver={(e) => {
          if (disabled) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          if (disabled) return;
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer.files);
        }}
        className={cn(
          'rounded-lg border-2 border-dashed p-6 text-center transition-colors',
          disabled
            ? 'border-border bg-surface-muted/40 text-foreground-muted'
            : dragging
              ? 'border-accent bg-accent/10'
              : 'border-border hover:border-accent/60',
        )}
      >
        <p className="text-sm">
          {disabled
            ? 'Select a collection to upload documents into.'
            : 'Drag files here, or'}
        </p>
        {!disabled && (
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="mt-2 rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted"
          >
            Choose files
          </button>
        )}
        <input
          ref={inputRef}
          type="file"
          multiple
          className="sr-only"
          aria-label="Choose files to upload"
          disabled={disabled}
          onChange={(e) => {
            handleFiles(e.target.files);
            // Reset so picking the same file again re-fires change.
            e.target.value = '';
          }}
        />
      </div>

      {entries.length > 0 && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-xs font-medium text-foreground-muted">Uploads</h3>
            {hasFinished && (
              <button
                type="button"
                onClick={clearFinished}
                className="text-xs text-foreground-muted underline hover:text-foreground"
              >
                Clear finished
              </button>
            )}
          </div>
          <ul className="space-y-1.5" aria-label="Upload progress">
            {entries.map((u) => (
              <li
                key={u.id}
                className="rounded-md border border-border px-3 py-2 text-sm"
                data-state={u.state}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate">{u.filename}</span>
                  <UploadStateTag state={u.state} progress={u.progress} />
                  {u.state !== 'uploading' && (
                    <button
                      type="button"
                      onClick={() => remove(u.id)}
                      aria-label={`Dismiss ${u.filename}`}
                      className="shrink-0 rounded px-1 text-foreground-muted hover:bg-surface-muted"
                    >
                      ✕
                    </button>
                  )}
                </div>
                {u.state === 'uploading' && (
                  <div
                    className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-muted"
                    role="progressbar"
                    aria-label={`Uploading ${u.filename}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(u.progress * 100)}
                  >
                    <div
                      className="h-full bg-accent transition-[width]"
                      style={{ width: `${Math.round(u.progress * 100)}%` }}
                    />
                  </div>
                )}
                {u.state === 'error' && (
                  <p role="alert" className="mt-1 text-xs text-danger">
                    {u.error}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function UploadStateTag({ state, progress }: { state: string; progress: number }) {
  if (state === 'done') return <span className="shrink-0 text-xs text-ok">Uploaded</span>;
  if (state === 'error') return <span className="shrink-0 text-xs text-danger">Failed</span>;
  return (
    <span className="shrink-0 text-xs text-foreground-muted" aria-live="polite">
      {Math.round(progress * 100)}%
    </span>
  );
}
