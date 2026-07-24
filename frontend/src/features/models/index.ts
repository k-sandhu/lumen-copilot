/**
 * Public surface of the model-registry slice (GET /models). Deliberately tiny and
 * dependency-free beyond `@/api`: chat, preferences, and assistants all import it,
 * so it must never import any of them back (FE-9 — that cycle is what kept the
 * markdown pipeline on the first-paint path).
 */
export { useModels, modelsKey } from './model/queries';
