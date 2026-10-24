/** @vitest-environment node */
import { beforeAll, describe, expect, it } from 'vitest';
import { build, type Rollup } from 'vite';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const normalizedModules = (chunk: Rollup.OutputChunk) =>
  Object.keys(chunk.modules).map((id) => id.replace(/\\/g, '/'));
const isHeavy = (id: string) =>
  id.endsWith('/features/chat/components/ChatView.tsx') ||
  id.endsWith('/lib/markdown.tsx') ||
  /node_modules\/(react-markdown|highlight\.js|rehype-|remark-|micromark)/.test(id);

describe('Assistants uses lightweight chat controls (#531)', () => {
  let chunks: Rollup.OutputChunk[] = [];
  let closure: Rollup.OutputChunk[] = [];
  beforeAll(async () => {
    const result = await build({
      root: frontendRoot,
      mode: 'production',
      logLevel: 'silent',
      build: { write: false },
    });
    const outputs = Array.isArray(result) ? result : [result];
    chunks = outputs
      .flatMap((output) => ('output' in output ? output.output : []))
      .filter((output): output is Rollup.OutputChunk => output.type === 'chunk');
    const route = chunks.find((chunk) =>
      normalizedModules(chunk).some((id) =>
        id.endsWith('/features/assistants/components/AssistantsPage.tsx'),
      ),
    );
    if (!route) throw new Error('AssistantsPage missing from production output');
    const byFile = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
    const seen = new Set<string>();
    const stack = [route.fileName];
    while (stack.length) {
      const file = stack.pop()!;
      if (seen.has(file)) continue;
      seen.add(file);
      for (const imported of byFile.get(file)?.imports ?? []) stack.push(imported);
    }
    closure = [...seen].flatMap((file) => byFile.get(file) ?? []);
    console.info(
      JSON.stringify({
        assistantsStaticChunks: closure.map((chunk) => chunk.name).sort(),
        rawBytes: closure.reduce((n, chunk) => n + Buffer.byteLength(chunk.code), 0),
        gzipBytes: closure.reduce((n, chunk) => n + gzipSync(chunk.code).byteLength, 0),
        heavyModuleCount: closure.flatMap(normalizedModules).filter(isHeavy).length,
      }),
    );
  }, 180_000);

  it('does not statically load ChatView or the Markdown pipeline', () => {
    const offenders = closure
      .filter((chunk) => normalizedModules(chunk).some(isHeavy))
      .map((chunk) => chunk.name);
    expect(offenders).toEqual([]);
  });

  it('still ships model selection and the shared chat store on the Assistants path', () => {
    const ids = closure.flatMap(normalizedModules);
    expect(ids.some((id) => id.endsWith('/features/chat/components/ModelPicker.tsx'))).toBe(true);
    expect(ids.some((id) => id.endsWith('/features/chat/model/chatStore.ts'))).toBe(true);
  });

  it('still emits ChatView and Markdown for actual chats and previews', () => {
    const ids = chunks.flatMap(normalizedModules);
    expect(ids.some((id) => id.endsWith('/features/chat/components/ChatView.tsx'))).toBe(true);
    expect(ids.some((id) => id.endsWith('/lib/markdown.tsx'))).toBe(true);
    expect(ids.some((id) => id.endsWith('/components/DocumentPreviewBody.tsx'))).toBe(true);
    expect(
      closure
        .flatMap(normalizedModules)
        .some((id) => id.endsWith('/components/DocumentPreviewBody.tsx')),
    ).toBe(false);
    expect(chunks.some((chunk) => chunk.dynamicImports.length > 0)).toBe(true);
  });
});
