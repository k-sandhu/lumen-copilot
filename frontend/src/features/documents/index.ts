/**
 * Public surface of the documents feature slice (issue #49). Routes and other
 * features import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { DocumentsPanel } from './components/DocumentsPanel';
export { CollectionsSidebar } from './components/CollectionsSidebar';
export { DocumentUpload } from './components/DocumentUpload';
export { DocumentList } from './components/DocumentList';
export { DocumentViewer } from './components/DocumentViewer';
export {
  useCollections,
  useCreateCollection,
  useUpdateCollection,
  useDeleteCollection,
  useDocuments,
  useDeleteDocument,
} from './model/queries';
export { useUploadStore } from './model/uploadStore';
export type { UploadEntry, UploadState } from './model/uploadStore';
