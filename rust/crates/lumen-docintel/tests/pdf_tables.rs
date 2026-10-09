mod pdf_support;
use lumen_docintel_core::{
    canonical::{self, BlockKind, HeaderRole},
    formats::pdf,
    runtime::Budget,
};
use pdf_support::{pdf_bytes, text};

#[test]
fn sparse_table_serialization_is_charged_even_when_visible_text_is_small() {
    use lumen_docintel_core::CoreError;
    use lumen_docintel_core::runtime::{Cancellation, Context, Runtime};
    let xs: Vec<_> = (0..=10).map(|i| 40 + i * 40).collect();
    let ys: Vec<_> = (0..=10).map(|i| 750 - i * 30).collect();
    let content = grid(&xs, &ys) + &text(50, 730, 12, "X");
    let input = pdf_bytes(&[content.as_bytes()], 0);
    let runtime = Runtime::new(2, 1).unwrap();
    let ctx = Context::new(Budget::default(), Cancellation::default()).unwrap();
    let doc = pdf::extract_with_context(&input, &ctx, &runtime).unwrap();
    assert_eq!(doc.blocks[0].table.as_ref().unwrap().cells.len(), 100);
    let budget = Budget {
        max_memory_bytes: ctx.stats().peak_accounted_bytes + 1024,
        ..Budget::default()
    };
    let ctx = Context::new(budget, Cancellation::default()).unwrap();
    assert!(matches!(
        pdf::extract_json(&input, &ctx, &runtime),
        Err(CoreError::Budget)
    ));
}
fn grid(xs: &[i32], ys: &[i32]) -> String {
    let mut lines = String::new();
    for x in xs {
        lines.push_str(&format!("{x} {} m {x} {} l S\n", ys[0], ys[ys.len() - 1]));
    }
    for y in ys {
        lines.push_str(&format!("{} {y} m {} {y} l S\n", xs[0], xs[xs.len() - 1]));
    }
    lines
}
fn body(y: i32, rows: &[(&str, &str)]) -> String {
    rows.iter()
        .enumerate()
        .map(|(i, (a, b))| {
            format!(
                "{}\n{}",
                text(50, y - i as i32 * 30, 12, a),
                text(250, y - i as i32 * 30, 12, b)
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}
#[test]
fn bordered_cells_headers_values_and_boxes() {
    let content = grid(&[40, 240, 440], &[740, 710, 680, 650])
        + &body(
            720,
            &[
                ("Region", "Mass"),
                ("North", "-120 kg*"),
                ("South", "+30 kg"),
            ],
        );
    let doc = pdf::extract(&pdf_bytes(&[content.as_bytes()], 0), Budget::default()).unwrap();
    let table = doc
        .blocks
        .iter()
        .find_map(|b| b.table.as_ref())
        .expect("bordered table");
    assert_eq!((table.rows, table.columns), (3, 2));
    assert_eq!(table.cells[0].header_role, HeaderRole::Column);
    assert!(
        table
            .cells
            .iter()
            .any(|c| c.row == 2 && c.column == 2 && c.text == "-120 kg*")
    );
    assert!(
        table
            .cells
            .iter()
            .all(|c| c.regions[0].number == Some(1) && c.regions[0].bbox.is_some())
    );
    let r = canonical::render(doc).unwrap();
    assert!(r.rendered_text.contains("C2 [Mass]=-120 kg*"));
    for (s, b) in r.spans.iter().zip(&r.document.blocks) {
        let slice: String = r
            .rendered_text
            .chars()
            .skip(s.char_start)
            .take(s.char_end - s.char_start)
            .collect();
        assert_eq!(slice, b.text);
    }
}

#[test]
fn table_insertion_keeps_surrounding_column_reading_order() {
    let content = text(40, 770, 22, "Title")
        + "\n"
        + &grid(&[40, 240, 440], &[730, 700, 670])
        + &body(710, &[("Region", "Mass"), ("North", "-120 kg")])
        + "\n"
        + &text(40, 600, 12, "LeftOne")
        + "\n"
        + &text(280, 600, 12, "RightOne")
        + "\n"
        + &text(40, 580, 12, "LeftTwo")
        + "\n"
        + &text(280, 580, 12, "RightTwo");
    let rendered = canonical::render(
        pdf::extract(&pdf_bytes(&[content.as_bytes()], 0), Budget::default()).unwrap(),
    )
    .unwrap();
    let positions: Vec<_> = [
        "Title", "[Table]", "LeftOne", "LeftTwo", "RightOne", "RightTwo",
    ]
    .iter()
    .map(|s| rendered.rendered_text.find(s).unwrap())
    .collect();
    assert!(positions.windows(2).all(|p| p[0] < p[1]));
}
#[test]
fn borderless_alignment_does_not_force_prose_columns_into_tables() {
    let data = body(
        700,
        &[
            ("Region", "Mass"),
            ("North", "-120 kg*"),
            ("South", "+30 kg"),
        ],
    );
    let doc = pdf::extract(&pdf_bytes(&[data.as_bytes()], 0), Budget::default()).unwrap();
    assert_eq!(
        doc.blocks
            .iter()
            .filter(|b| b.kind == BlockKind::Table)
            .count(),
        1
    );
    for rows in [
        [
            ("LeftOne", "RightOne"),
            ("LeftTwo", "RightTwo"),
            ("LeftThree", "RightThree"),
        ],
        [
            ("Chapter one", "Other chapter"),
            ("Long prose", "More prose"),
            ("Final words", "Closing words"),
        ],
    ] {
        let prose = body(700, &rows);
        let doc = pdf::extract(&pdf_bytes(&[prose.as_bytes()], 0), Budget::default()).unwrap();
        assert!(doc.blocks.iter().all(|b| b.table.is_none()));
    }
}
#[test]
fn missing_inner_ruling_creates_spanning_origin_cell() {
    let content = grid(&[40, 440], &[740, 710, 680])
        + "240 710 m 240 680 l S\n"
        + &text(50, 720, 12, "Shared heading")
        + "\n"
        + &body(690, &[("North", "-120 kg")]);
    let doc = pdf::extract(&pdf_bytes(&[content.as_bytes()], 0), Budget::default()).unwrap();
    let table = doc.blocks.iter().find_map(|b| b.table.as_ref()).unwrap();
    assert!(
        table.cells.iter().any(|c| c.row == 1
            && c.column == 1
            && c.column_span == 2
            && c.text == "Shared heading")
    );
}
#[test]
fn consecutive_page_tables_join_with_cell_provenance_and_exact_page_segments() {
    let first = grid(&[40, 240, 440], &[160, 130, 100])
        + &body(140, &[("Region", "Mass"), ("North", "-120 kg")]);
    let second = grid(&[40, 240, 440], &[750, 720, 690])
        + &body(730, &[("Region", "Mass"), ("South", "+30 kg")]);
    let doc = pdf::extract(
        &pdf_bytes(&[first.as_bytes(), second.as_bytes()], 0),
        Budget::default(),
    )
    .unwrap();
    let tables: Vec<_> = doc.blocks.iter().filter_map(|b| b.table.as_ref()).collect();
    assert_eq!(tables.len(), 1);
    assert_eq!(tables[0].rows, 4);
    assert!(
        tables[0]
            .cells
            .iter()
            .any(|c| c.text == "+30 kg" && c.regions[0].number == Some(2))
    );
    let rendered = canonical::render(doc).unwrap();
    let parts = &rendered.document.source_parts;
    assert_eq!(parts.len(), 2);
    assert!(parts[0].char_end <= parts[1].char_start);
    let page1: String = rendered
        .rendered_text
        .chars()
        .skip(parts[0].char_start)
        .take(parts[0].char_end - parts[0].char_start)
        .collect();
    let page2: String = rendered
        .rendered_text
        .chars()
        .skip(parts[1].char_start)
        .take(parts[1].char_end - parts[1].char_start)
        .collect();
    assert!(page1.contains("North") && !page1.contains("South"));
    assert!(page2.contains("South") && !page2.contains("North"));
}

#[test]
fn unpainted_paths_prose_and_different_headers_are_not_tables_or_continuations() {
    let rows = [
        ("LeftOne", "RightOne"),
        ("LeftTwo", "RightTwo"),
        ("LeftThree", "RightThree"),
    ];
    let unpainted =
        grid(&[40, 240, 440], &[740, 710, 680, 650]).replace(" l S", " l n") + &body(720, &rows);
    let doc = pdf::extract(&pdf_bytes(&[unpainted.as_bytes()], 0), Budget::default()).unwrap();
    assert!(doc.blocks.iter().all(|b| b.table.is_none()));
    let first = grid(&[40, 240, 440], &[160, 130, 100])
        + &body(140, &[("Region", "Mass"), ("North", "-120 kg")]);
    let second = grid(&[40, 240, 440], &[750, 720, 690])
        + &body(730, &[("Region", "Cost"), ("South", "+30 USD")]);
    let doc = pdf::extract(
        &pdf_bytes(&[first.as_bytes(), second.as_bytes()], 0),
        Budget::default(),
    )
    .unwrap();
    assert_eq!(doc.blocks.iter().filter(|b| b.table.is_some()).count(), 2);
    let interrupted = first + "\n" + &text(40, 50, 12, "Intervening prose");
    let same = grid(&[40, 240, 440], &[750, 720, 690])
        + &body(730, &[("Region", "Mass"), ("South", "+30 kg")]);
    let doc = pdf::extract(
        &pdf_bytes(&[interrupted.as_bytes(), same.as_bytes()], 0),
        Budget::default(),
    )
    .unwrap();
    assert_eq!(doc.blocks.iter().filter(|b| b.table.is_some()).count(), 2);
}

#[test]
fn vertical_spans_and_sparse_positions_preserve_origin_cells() {
    let content = grid(&[40, 240, 440], &[740, 650])
        + "40 710 m 440 710 l S\n240 680 m 440 680 l S\n"
        + &body(720, &[("Region", "Mass"), ("North", "-120 kg")])
        + "\n"
        + &text(250, 660, 12, "+30 kg");
    let doc = pdf::extract(&pdf_bytes(&[content.as_bytes()], 0), Budget::default()).unwrap();
    let table = doc.blocks.iter().find_map(|b| b.table.as_ref()).unwrap();
    assert!(
        table
            .cells
            .iter()
            .any(|c| c.row == 2 && c.column == 1 && c.row_span == 2 && c.text == "North")
    );
    let blank = grid(&[40, 240, 440], &[740, 710, 680])
        + &body(720, &[("Region", "Mass")])
        + "\n"
        + &text(250, 690, 12, "-120 kg");
    let doc = pdf::extract(&pdf_bytes(&[blank.as_bytes()], 0), Budget::default()).unwrap();
    let table = doc.blocks.iter().find_map(|b| b.table.as_ref()).unwrap();
    assert!(
        table
            .cells
            .iter()
            .any(|c| c.row == 2 && c.column == 1 && c.text.is_empty())
    );
}

proptest::proptest! {
    #[test]
    fn table_values_exact_offsets_and_page_boxes_are_stable(value in -999i32..999) {
        let mass=format!("{value} kg*");
        let content=grid(&[40,240,440],&[740,710,680])+&body(720,&[("Region","Mass"),("North",&mass)]);
        let rendered=canonical::render(pdf::extract(&pdf_bytes(&[content.as_bytes()],0),Budget::default()).unwrap()).unwrap();
        let table=rendered.document.blocks[0].table.as_ref().unwrap();
        proptest::prop_assert!(table.cells.iter().any(|c|c.text==mass&&c.column==2&&c.row==2));
        let span=&rendered.spans[0];let text:String=rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect();
        proptest::prop_assert_eq!(text,rendered.document.blocks[0].text.clone());
        for cell in &table.cells {let region=&cell.regions[0];let bbox=region.bbox.as_ref().unwrap();proptest::prop_assert_eq!(region.number,Some(1));proptest::prop_assert!(bbox.x0>=40.&&bbox.x1<=440.&&bbox.y0>=680.&&bbox.y1<=740.);}
    }
}
