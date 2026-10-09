"""Native facade isolation and immutable taxonomy-rule references."""

import json

import pytest

from app.classification.rules import load_rules
from app.domain.classification_features import ClassificationFeatures
from app.ingestion import native


def test_rule_release_targets_existing_leaf() -> None:
    assert load_rules()["taxonomy_version"] == "1.0.0"


def test_full_source_cannot_enter_classification_payload() -> None:
    with pytest.raises(ValueError):
        ClassificationFeatures.from_json(
            json.dumps({"schema_version": 1, "rendered_text": "secret"})
        )


def test_native_unavailable_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(native, "_extension", lambda: None)
    with pytest.raises(native.NativeUnavailableError):
        native.classification_features(
            None,
            tokenizer_json="{}",
            rules_json="{}",
            max_tokens=10,
            max_excerpt_chars=10,
            format="text",
        )


def test_native_features_and_batch_binding() -> None:
    pytest.importorskip("lumen_docintel")
    tokenizer = json.dumps(
        {
            "version": "1.0",
            "truncation": None,
            "padding": None,
            "added_tokens": [],
            "normalizer": None,
            "pre_tokenizer": {"type": "Whitespace"},
            "post_processor": None,
            "decoder": None,
            "model": {"type": "WordLevel", "vocab": {"[UNK]": 0}, "unk_token": "[UNK]"},
        }
    )
    rules = json.dumps(load_rules())
    document = native.render_canonical(
        json.dumps({"blocks": [{"id": "b", "text": "Synthetic 🧾"}]})
    )
    result = native.classification_features(
        document,
        tokenizer_json=tokenizer,
        rules_json=rules,
        max_tokens=8,
        max_excerpt_chars=32,
        format="text",
    )
    assert result.payload()["excerpts"][0]["text"] == "Synthetic 🧾"
    batch = native.NativeExecutor(threads=2, max_documents=2).classification_batch(
        (document, document),
        tokenizer_json=tokenizer,
        rules_json=rules,
        settings_json=json.dumps(
            {"max_tokens": 8, "max_excerpt_chars": 32, "format": "text", "override_path": None}
        ),
    )
    assert batch == (result, result)
    assert native.classification_token_count("Synthetic 🧾", tokenizer) > 0
