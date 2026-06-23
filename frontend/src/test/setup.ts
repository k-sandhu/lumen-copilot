import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// Unmount React trees between tests to avoid cross-test leakage.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// A handful of suites run in the `node` environment (e.g. the build-artifact
// guard `src/build/*.test.ts`), where `window` and the browser globals below do
// not exist. This setup file runs for EVERY suite, so guard the DOM stubs behind
// a window check — they're only meaningful under jsdom.
const hasWindow = typeof window !== 'undefined';

// jsdom lacks matchMedia (used by the theme store). Provide a no-op stub.
if (hasWindow && !window.matchMedia) {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

// jsdom lacks ResizeObserver, which Radix primitives (ScrollArea, Tooltip) use.
// Provide a no-op stub so components mount in tests.
if (!('ResizeObserver' in globalThis)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

// jsdom lacks URL.createObjectURL / revokeObjectURL, which the document viewer
// uses to render bearer-fetched content as a blob: URL. Provide a deterministic
// stub so components that load document content mount in tests.
if (typeof URL.createObjectURL !== 'function') {
  let counter = 0;
  URL.createObjectURL = () => `blob:test/${++counter}`;
}
if (typeof URL.revokeObjectURL !== 'function') {
  URL.revokeObjectURL = () => {};
}
