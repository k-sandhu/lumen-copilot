"""ADR-0026 / #663: optional bridge, panic containment and GIL handshake."""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_python_ingestion_imports_without_extension() -> None:
    # A fresh process deliberately rejects extension imports even if installed.
    source = """
import sys
class RejectNative:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lumen_docintel':
            raise ImportError('extension deliberately absent')
sys.meta_path.insert(0, RejectNative())
from app.ingestion.parsers import parse_document
from app.ingestion.native import native_available
assert not native_available()
assert parse_document(b'hello', mime_type='text/plain') == 'hello'
"""
    subprocess.run([sys.executable, "-c", source], check=True, timeout=30)


def test_bridge_import_and_typed_errors() -> None:
    native = pytest.importorskip("lumen_docintel")
    assert native.core_version() == "0.1.0"
    with pytest.raises(native.DocIntelInvalidInputError):
        native._test_error()
    with pytest.raises(native.DocIntelPanicError, match="native computation panicked"):
        native._test_panic()
    assert native.core_version() == "0.1.0"  # Same process remains usable.


def test_gil_release_with_handshake() -> None:
    native = pytest.importorskip("lumen_docintel")
    gate = native._Handshake()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(native._test_wait, gate)
        try:
            # True means native is waiting, not already timed out/completed.
            assert gate.wait_entered()
            assert not running.done()
        finally:
            gate.release()
        assert running.result(timeout=10) is None
