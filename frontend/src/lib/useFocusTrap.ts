/**
 * useFocusTrap — the ONE focus-trap for every `aria-modal` surface in the app.
 *
 * It generalizes the focus-on-open + Escape + restore-to-opener effect that the
 * modal dialogs used to hand-roll individually (ProvenanceDrawer, ConfirmDialog,
 * AddSourceModal, DocumentViewer) and adds the piece each was missing: it traps
 * Tab / Shift+Tab inside the dialog so keyboard focus can't escape to the page
 * behind the overlay. There is intentionally NO focus-trap dependency
 * (`focus-trap-react`, `@radix-ui` Dialog, …) — this builds on the in-repo
 * pattern only (frontend/AGENTS.md "no new deps").
 *
 * On `open` becoming true it stores `document.activeElement`, moves focus inside
 * the container (an explicit `initialFocus` target if given, else the first
 * focusable element, else the container itself), and listens for keydown to
 * (a) call `onClose` on Escape and (b) wrap focus at the container's edges on
 * Tab. On close/unmount it removes the listener and restores focus to the opener.
 */
import { useEffect, type RefObject } from 'react';

/** Elements that can hold keyboard focus inside a dialog (mirrors the WAI set). */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

interface FocusTrapOptions {
  /** Element to focus on open instead of the first focusable child. */
  initialFocus?: RefObject<HTMLElement | null>;
  /** When true the trap is inert (no focus move, no key handling). */
  disabled?: boolean;
}

function focusableWithin(container: HTMLElement): HTMLElement[] {
  // Skip elements explicitly hidden from layout/AT. We deliberately do NOT filter
  // on `offsetParent`/`getBoundingClientRect` — jsdom has no layout, so those
  // would wrongly drop every element under test; a dialog's controls are visible
  // by construction.
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('hidden') && el.getAttribute('aria-hidden') !== 'true',
  );
}

/**
 * Trap focus within `ref` while `open` is true.
 *
 * @param open      Whether the modal surface is mounted/visible.
 * @param ref       The dialog container (the element with `role="dialog"`).
 * @param onClose   Called when Escape is pressed (the caller may gate it on busy).
 * @param opts      `initialFocus` target and a `disabled` escape hatch.
 */
export function useFocusTrap(
  open: boolean,
  ref: RefObject<HTMLElement | null>,
  onClose: () => void,
  opts: FocusTrapOptions = {},
): void {
  const { initialFocus, disabled = false } = opts;

  useEffect(() => {
    if (!open || disabled) return;
    if (typeof document === 'undefined' || typeof window === 'undefined') return;

    const container = ref.current;
    if (!container) return;

    const lastFocused = (document.activeElement as HTMLElement | null) ?? null;

    // Move focus inside on the next frame so children are mounted/measurable.
    const frame = window.requestAnimationFrame(() => {
      const target = initialFocus?.current ?? focusableWithin(container)[0] ?? container;
      target.focus();
    });

    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;

      const focusable = focusableWithin(container);
      if (focusable.length === 0) {
        // Nothing focusable inside — keep focus on the container itself.
        e.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;

      if (e.shiftKey) {
        // Wrapping backwards off the first element (or from outside the trap).
        if (active === first || !container.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !container.contains(active)) {
        // Wrapping forwards off the last element (or from outside the trap).
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey);
      lastFocused?.focus?.();
    };
    // initialFocus is a stable ref object; only re-run on open/disabled/onClose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, disabled, onClose]);
}
