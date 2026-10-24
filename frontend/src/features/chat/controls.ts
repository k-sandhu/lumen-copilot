/**
 * Lightweight public chat entry point for other feature slices (#531).
 * These exports must not depend on ChatView, streaming, or content rendering.
 * Re-export the existing instances so controls and session state stay shared.
 */
export { ModelPicker, type ModelPickerProps } from './components/ModelPicker';
export { useChatStore } from './model/chatStore';
