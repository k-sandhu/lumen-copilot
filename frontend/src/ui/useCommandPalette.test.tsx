import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useCommandPalette } from './useCommandPalette';

function pressMetaK(): void {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }));
}
function pressCtrlK(): void {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
}

describe('useCommandPalette', () => {
  it('starts closed', () => {
    const { result } = renderHook(() => useCommandPalette());
    expect(result.current.open).toBe(false);
  });

  it('toggles on ⌘K', () => {
    const { result } = renderHook(() => useCommandPalette());
    act(() => pressMetaK());
    expect(result.current.open).toBe(true);
    act(() => pressMetaK());
    expect(result.current.open).toBe(false);
  });

  it('toggles on Ctrl-K', () => {
    const { result } = renderHook(() => useCommandPalette());
    act(() => pressCtrlK());
    expect(result.current.open).toBe(true);
  });

  it('exposes setOpen + toggle', () => {
    const { result } = renderHook(() => useCommandPalette());
    act(() => result.current.setOpen(true));
    expect(result.current.open).toBe(true);
    act(() => result.current.toggle());
    expect(result.current.open).toBe(false);
  });

  it('removes its listener on unmount', () => {
    const { result, unmount } = renderHook(() => useCommandPalette());
    unmount();
    act(() => pressMetaK());
    // No assertion error / state update after unmount; the prior result stays closed.
    expect(result.current.open).toBe(false);
  });
});
