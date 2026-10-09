"""#678 generated containers and explicit, content-safe refusal."""

from __future__ import annotations

import struct

import pytest

from app.core.config import Settings
from app.ingestion import native


def compound(stream: str) -> bytes:
    """Construct a CFB v3 container with one stream; no third-party fixture."""
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header, 24, 0x3E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 0, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    struct.pack_into("<109I", header, 76, 1, *([0xFFFFFFFF] * 108))
    directory = bytearray(512)
    for offset, name, kind in ((0, "Root Entry", 5), (128, stream, 2)):
        encoded = (name + "\0").encode("utf-16le")
        directory[offset : offset + len(encoded)] = encoded
        struct.pack_into(
            "<HBBIII",
            directory,
            offset + 64,
            len(encoded),
            kind,
            1,
            0xFFFFFFFF,
            0xFFFFFFFF,
            1 if kind == 5 else 0xFFFFFFFF,
        )
        struct.pack_into(
            "<IQ", directory, offset + 116, 0xFFFFFFFE if kind == 5 else 2, 0 if kind == 5 else 4096
        )
    fat = struct.pack(
        "<128I", 0xFFFFFFFE, 0xFFFFFFFD, *range(3, 10), 0xFFFFFFFE, *([0xFFFFFFFF] * 118)
    )
    return bytes(header + directory + fat + bytearray(4096))


def test_default_off_never_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> None:
        raise AssertionError("disabled legacy candidate imported extension")

    monkeypatch.setattr(native, "_extension", forbidden)
    with pytest.raises(native.LegacyOfficeDisabledError):
        native.reject_legacy_office(b"untrusted", format_name="doc", settings=Settings())
    assert (
        native.legacy_office_route(
            settings=Settings(native_doc_cutover=True, native_ppt_cutover=True)
        )
        == "unsupported"
    )


def test_actual_bridge_returns_safe_typed_outcome() -> None:
    extension = pytest.importorskip("lumen_docintel")
    assert hasattr(extension, "reject_legacy_office"), "rebuild native extra for #678"
    settings = Settings(native_doc_accept=True, native_ppt_accept=True)
    for stream, fmt in (("WordDocument", "doc"), ("PowerPoint Document", "ppt")):
        data = compound(stream)
        assert native.detect_content(data).format == fmt
        with pytest.raises(extension.DocIntelUnsupportedLegacyFormatError) as exc:
            native.reject_legacy_office(data, format_name=fmt, settings=settings)
        assert isinstance(exc.value, extension.DocIntelUnsupportedError)
        assert exc.value.code == "unsupported_legacy_format"
        assert str(exc.value) == "unsupported_legacy_format"


def test_all_legacy_switches_default_off() -> None:
    settings = Settings()
    for fmt in ("doc", "ppt"):
        for flag in ("accept", "shadow", "cutover"):
            assert getattr(settings, f"native_{fmt}_{flag}") is False


def test_shadow_refusal_retains_python_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusal(Exception):
        code = "unsupported_legacy_format"

    def refuse(*args: object, **kwargs: object) -> None:
        raise Refusal("private native payload")

    monkeypatch.setattr(native, "reject_legacy_office", refuse)
    result, diagnostics = native.shadow_legacy_office(
        b"untrusted",
        format_name="doc",
        python_text="retained",
        settings=Settings(native_doc_accept=True, native_doc_shadow=True),
    )
    assert result == "retained"
    assert diagnostics == {"candidate_outcome": "unsupported_legacy_format"}
