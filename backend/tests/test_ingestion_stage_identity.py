"""Parser build and library changes invalidate cached extraction."""

from app.ingestion.stage_identity import parser_identity


def test_parser_identity_carries_build_and_runtime_without_local_paths(monkeypatch):
    monkeypatch.setattr("app.ingestion.stage_identity.version", lambda name: "1.0")
    first = parser_identity("application/pdf")
    assert first["libraries"] == {"pypdf": "1.0"}
    assert len(first["parser_sha256"]) == 64
    assert "path" not in str(first)
    monkeypatch.setattr("app.ingestion.stage_identity.version", lambda name: "2.0")
    assert first != parser_identity("application/pdf")


def test_plain_text_identity_requires_no_parser_library(monkeypatch):
    def reject(name):
        raise AssertionError("unexpected parser dependency")

    monkeypatch.setattr("app.ingestion.stage_identity.version", reject)
    assert parser_identity("text/plain; charset=utf-8")["libraries"] == {}
