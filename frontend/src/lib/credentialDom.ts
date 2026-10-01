import { useCallback, useRef } from 'react';

/** Clear every native representation, including React's reflected default. */
export function scrubCredentialInput(input: HTMLInputElement | HTMLTextAreaElement): void {
  input.value = '';
  input.defaultValue = '';
  input.removeAttribute('value');
  input.removeAttribute('defaultvalue');
  if (
    input instanceof HTMLInputElement &&
    (input.type === 'password' || /^(current-password|new-password)$/.test(input.autocomplete))
  ) {
    input.type = 'password';
  }
}

/**
 * A form-local DOM holder. Keep removed descendants until the next boundary so
 * clearing also reaches manager mirrors detached before the form itself. Draft
 * strings and node references are discarded after the scrub, never persisted.
 */
function credentialDomScope(root: HTMLElement) {
  const retained = new Set<Node>();
  const drafts = new Set<string>();
  // Authored labels/semantics can coincidentally contain a typed word such as
  // "password". They are not copies of the draft; preserve unchanged originals.
  const originalAttributes = new WeakMap<Element, Map<string, string>>();
  const originalText = new WeakMap<Node, string | null>();
  function snapshotOriginal(node: Node) {
    if (node instanceof Element)
      originalAttributes.set(
        node,
        new Map([...node.attributes].map(({ name, value }) => [name, value])),
      );
    originalText.set(node, node.nodeValue);
    for (const child of node.childNodes) snapshotOriginal(child);
  }
  snapshotOriginal(root);
  function rememberValue(value: string) {
    if (value) drafts.add(value);
  }
  function remember(node: Node) {
    retained.add(node);
    for (const child of node.childNodes) remember(child);
  }
  function record(records: MutationRecord[]) {
    for (const record of records) {
      remember(record.target);
      for (const node of record.addedNodes) remember(node);
      for (const node of record.removedNodes) remember(node);
    }
  }
  const observer = new MutationObserver(record);
  remember(root);
  observer.observe(root, { subtree: true, childList: true, attributes: true, characterData: true });
  const rememberDraft = (event: Event) => {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
      rememberValue(event.target.value);
    }
  };
  root.addEventListener('input', rememberDraft, true);

  function clear() {
    // Flush synchronously: an add/remove and logout can occur in the same task,
    // before the observer's normal microtask callback gets a chance to run.
    record(observer.takeRecords());
    remember(root);
    for (const node of retained) {
      if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
        for (const value of [node.value, node.defaultValue]) rememberValue(value);
      }
    }
    // Editing or erasing a draft does not make an earlier copied version safe.
    // Match every observed value; unchanged authored semantics are protected by
    // the original snapshots above, rather than by dropping shorter secrets.
    const secrets = [...drafts];
    const containsSecret = (value: string) => secrets.some((secret) => value.includes(secret));
    for (const node of retained) {
      if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)
        scrubCredentialInput(node);
      if (node instanceof Element) {
        for (const attribute of [...node.attributes]) {
          if (
            containsSecret(attribute.value) &&
            originalAttributes.get(node)?.get(attribute.name) !== attribute.value
          )
            node.removeAttribute(attribute.name);
        }
      } else if (
        node.nodeValue &&
        containsSecret(node.nodeValue) &&
        originalText.get(node) !== node.nodeValue
      )
        node.nodeValue = '';
    }
    drafts.clear();
    retained.clear();
    // Do not retain our own scrub mutations (or their detached targets).
    observer.takeRecords();
    remember(root);
  }

  function dispose() {
    clear();
    observer.disconnect();
    root.removeEventListener('input', rememberDraft, true);
    retained.clear();
    drafts.clear();
  }
  return { clear, dispose };
}

/** Attach to a credential form/primitive; null-ref teardown is synchronous. */
export function useCredentialDomCleanup() {
  const scope = useRef<ReturnType<typeof credentialDomScope> | null>(null);
  const rememberRoot = useCallback((root: HTMLElement | null) => {
    scope.current?.dispose();
    scope.current = root ? credentialDomScope(root) : null;
  }, []);
  const clearDom = useCallback(() => scope.current?.clear(), []);
  return { rememberRoot, clearDom };
}
