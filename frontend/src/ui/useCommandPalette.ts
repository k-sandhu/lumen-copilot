/**
 * useCommandPalette — wires the ⌘K / Ctrl-K global shortcut to open/close state
 * for the CommandPalette (DESIGN.md §6). Self-contained so the palette is opt-in
 * and carries its own keybinding; consumers render <CommandPalette open … /> and
 * pass these handlers. Escape-to-close is owned by the palette itself.
 */
import { useCallback, useEffect, useState } from 'react';

export interface CommandPaletteApi {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

export function useCommandPalette(): CommandPaletteApi {
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((v) => !v), []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      // ⌘K (mac) / Ctrl-K (win/linux) toggles the palette.
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggle]);

  return { open, setOpen, toggle };
}
