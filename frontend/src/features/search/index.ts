/**
 * Public surface of the search feature slice (issue #84). Routes and other
 * features import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { SearchScreen } from './components/SearchScreen';
export { SearchComposer } from './components/SearchComposer';
export { SearchResultRow } from './components/SearchResultRow';
export { DirectAnswerBlock } from './components/DirectAnswerBlock';
export { TrimNotice } from './components/TrimNotice';
export { useSearch, searchKey } from './model/queries';
