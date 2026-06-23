/**
 * useDisclosure (issue #110) — a tiny popover controller for the shell menus
 * (appearance, account). Owns open state, closes on outside-click and Escape, and
 * returns focus to the trigger on close. Kept local to the shell; the kit already
 * ships a richer pattern for the command palette, but the menus only need this.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export interface DisclosureApi {
  open: boolean;
  toggle: () => void;
  close: () => void;
  triggerRef: React.RefObject<HTMLButtonElement>;
  menuRef: React.RefObject<HTMLDivElement>;
}

export function useDisclosure(): DisclosureApi {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((v) => !v), []);

  useEffect(() => {
    if (!open) return undefined;

    const onPointerDown = (e: PointerEvent): void => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return { open, toggle, close, triggerRef, menuRef };
}
