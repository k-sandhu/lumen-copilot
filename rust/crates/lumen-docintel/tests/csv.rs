use lumen_docintel_core::{
    CoreError,
    canonical::render,
    formats::{common::Limits, csv::parse},
};
use proptest::prelude::*;

#[test]
fn dialects_labels_locations_and_inert_formulas() {
    for delimiter in [',', ';', '\t', '|'] {
        let input = format!("Name{delimiter}Amount\n\"café\n東京\"{delimiter}=1+2\n");
        let doc = parse(input.as_bytes(), Limits::default()).unwrap();
        let block = &doc.blocks[0];
        let table = block.table.as_ref().unwrap();
        assert_eq!(table.rows, 2);
        assert_eq!(table.cells[2].text, "café\n東京");
        assert_eq!(table.cells[3].text, "=1+2");
        assert!(table.cells[3].formula.is_none());
        assert!(block.text.contains("[Name]=café\n東京"));
        assert_eq!(
            table.cells[2].regions[0].name.as_deref(),
            Some("record:2;line:2")
        );
        assert_eq!(
            doc.generation.diagnostics.as_ref().unwrap()["delimiter"],
            delimiter.to_string()
        );
        render(doc).unwrap();
    }
}

#[test]
fn ragged_numeric_and_budget_negatives() {
    let doc = parse(b"Name,Amount\na,12\nb\n", Limits::default()).unwrap();
    assert_eq!(doc.generation.outcome.as_deref(), Some("partial"));
    assert_eq!(doc.generation.diagnostics.unwrap()["ragged_rows"][0], 3);
    let doc = parse(b"1,2\n3,4", Limits::default()).unwrap();
    assert!(doc.blocks[0].text.contains("[C1]=1"));
    for field in ["input", "output", "memory", "work"] {
        let mut limits = Limits::default();
        match field {
            "input" => limits.budget.max_input_bytes = 1,
            "output" => limits.budget.max_output_chars = 1,
            "memory" => limits.budget.max_memory_bytes = 1,
            _ => limits.budget.max_work_units = 1,
        }
        assert_eq!(parse(b"A,B\n1,2", limits), Err(CoreError::Budget));
    }
    assert_eq!(
        parse(b"%PDF-1.7\n", Limits::default()),
        Err(CoreError::Unsupported)
    );
}

proptest! {
    #[test]
    fn unicode_spans_and_row_coordinates(values in prop::collection::vec("[a-zé東京🦀]{1,12}", 1..30)) {
        let mut bytes = String::from("Label,Value\n");
        for (n,value) in values.iter().enumerate() { bytes.push_str(&format!("{value},{n}\n")); }
        let rendered = render(parse(bytes.as_bytes(), Limits::default()).unwrap()).unwrap();
        for (span, block) in rendered.spans.iter().zip(&rendered.document.blocks) {
            let slice: String = rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect();
            prop_assert_eq!(slice, block.text.clone());
        }
        let cells = &rendered.document.blocks[0].table.as_ref().unwrap().cells;
        for (n,value) in values.iter().enumerate() {
            prop_assert_eq!(&cells[2*(n+1)].text, value);
            prop_assert_eq!(cells[2*(n+1)].row, n+2);
        }
    }
}
