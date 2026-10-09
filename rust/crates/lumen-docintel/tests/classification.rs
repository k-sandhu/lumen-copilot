use lumen_docintel_core::canonical::{Block, BlockKind, Document, Generation};
use lumen_docintel_core::classification::{FeatureSettings, RuleSet, classify_features};
use lumen_docintel_core::runtime::{Budget, Cancellation, Context};
use tokenizers::Tokenizer;

fn tokenizer() -> Tokenizer {
    Tokenizer::from_bytes(r#"{"version":"1.0","truncation":null,"padding":null,"added_tokens":[],"normalizer":null,"pre_tokenizer":{"type":"Whitespace"},"post_processor":null,"decoder":null,"model":{"type":"WordLevel","vocab":{"[UNK]":0},"unk_token":"[UNK]"}}"#).unwrap()
}
fn document(text: &str) -> Document {
    Document {
        blocks: vec![Block {
            id: "title".into(),
            kind: BlockKind::Heading,
            text: text.into(),
            heading_level: Some(1),
            ..Default::default()
        }],
        generation: Generation {
            extraction_id: Some("synthetic-generation".into()),
            ..Default::default()
        },
        ..Default::default()
    }
}
fn rules() -> RuleSet {
    serde_json::from_str(r#"{"taxonomy_version":"1.0.0","rules_version":"1","rules":[{"id":"synthetic","path":"forms/tax/w2","all_signatures":["form w-2","wage and tax statement"]}]}"#).unwrap()
}
#[test]
fn bounded_unicode_excerpt_and_rule_provenance() {
    let ctx = Context::new(Budget::default(), Cancellation::default()).unwrap();
    let doc = document("Form W-2 Wage and Tax Statement 🧾 extra evidence");
    let settings = FeatureSettings {
        max_tokens: 8,
        max_excerpt_chars: 32,
        format: "text".into(),
        override_path: None,
    };
    let out = classify_features(doc, &tokenizer(), &settings, &rules(), &ctx).unwrap();
    assert!(out.token_count <= 8);
    assert!(
        out.excerpts
            .iter()
            .map(|e| e.text.chars().count())
            .sum::<usize>()
            <= 32
    );
    assert_eq!(out.rule_path.as_deref(), Some("forms/tax/w2"));
    assert_eq!(out.rule_ids, vec!["synthetic"]);
    assert_eq!(out.extraction_id.as_deref(), Some("synthetic-generation"));
    for e in &out.excerpts {
        assert_eq!(
            e.text,
            out.rendered_text
                .chars()
                .skip(e.char_start)
                .take(e.char_end - e.char_start)
                .collect::<String>()
        );
    }
}
#[test]
fn conflicting_rules_and_override_defer_without_guessing() {
    for override_path in [None, Some("user/choice".into())] {
        let mut r = rules();
        let mut conflict = r.rules[0].clone();
        conflict.id = "conflict".into();
        conflict.path = "other/other/other".into();
        r.rules.push(conflict);
        let ctx = Context::new(Budget::default(), Cancellation::default()).unwrap();
        let out = classify_features(
            document("Form W-2 Wage and Tax Statement"),
            &tokenizer(),
            &FeatureSettings {
                max_tokens: 64,
                max_excerpt_chars: 256,
                format: "text".into(),
                override_path,
            },
            &r,
            &ctx,
        )
        .unwrap();
        assert!(out.rule_path.is_none());
    }
}
#[test]
fn cancellation_and_memory_limits_fail_closed() {
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert!(
        classify_features(
            document("evidence"),
            &tokenizer(),
            &FeatureSettings {
                max_tokens: 8,
                max_excerpt_chars: 32,
                format: "text".into(),
                override_path: None
            },
            &rules(),
            &ctx
        )
        .is_err()
    );
}

#[test]
fn ordered_parallel_documents_have_independent_budgets() {
    use lumen_docintel_core::runtime::Runtime;
    let runtime = Runtime::new(2, 2).unwrap();
    let documents: Vec<String> = ["first 🧾", "second", "third"]
        .iter()
        .map(|s| serde_json::to_string(&document(s)).unwrap())
        .collect();
    let settings = serde_json::to_string(&FeatureSettings {
        max_tokens: 8,
        max_excerpt_chars: 32,
        format: "text".into(),
        override_path: None,
    })
    .unwrap();
    let results = runtime.classification_batch(
        &documents,
        &tokenizer().to_string(false).unwrap(),
        &settings,
        &serde_json::to_string(&rules()).unwrap(),
        Budget::default(),
        Cancellation::default(),
    );
    for (result, expected) in results.into_iter().zip(["first 🧾", "second", "third"]) {
        let value: serde_json::Value = serde_json::from_str(&result.unwrap()).unwrap();
        assert_eq!(value["excerpts"][0]["text"], expected);
        assert!(value.get("rendered_text").is_none());
        assert!(value["language"].is_null());
        assert!(value["scanned_ratio"].is_null());
    }
    let budget = Budget {
        max_memory_bytes: 1,
        ..Budget::default()
    };
    assert!(
        runtime
            .classification_batch(
                &documents,
                "{}",
                &settings,
                "{}",
                budget,
                Cancellation::default()
            )
            .iter()
            .all(Result::is_err)
    );
}
