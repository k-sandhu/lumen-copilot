use lumen_docintel_core::{
    CoreError,
    canonical::{Block, BlockKind, Cell, Document, Table, render},
};
use proptest::prelude::*;
fn document(cells: Vec<Cell>, rows: usize, columns: usize) -> Document {
    Document {
        blocks: vec![Block {
            id: "table".to_owned(),
            kind: BlockKind::Table,
            text: "table 😀".to_owned(),
            table: Some(Table {
                rows,
                columns,
                cells,
                caption: None,
            }),
            ..Block::default()
        }],
        ..Document::default()
    }
}
fn cell(row: usize, column: usize, row_span: usize, column_span: usize) -> Cell {
    Cell {
        row,
        column,
        row_span,
        column_span,
        ..Cell::default()
    }
}
#[test]
fn valid_long_vertical_merge_and_thousand_cell_workbook_render() {
    let mut cells = vec![cell(1, 1, 1000, 1)];
    cells.extend((1..=1000).map(|r| cell(r, 2, 1, 1)));
    assert!(render(document(cells, 1000, 2)).is_ok());
    assert!(
        render(document(
            (1..=1000).map(|r| cell(r, 1, 1, 1)).collect(),
            1000,
            1
        ))
        .is_ok()
    );
}
#[test]
fn overlaps_and_invalid_spans_still_fail_closed() {
    for cells in [
        vec![cell(1, 1, 2, 2), cell(2, 2, 1, 1)],
        vec![cell(1, 1, 1, 1), cell(1, 1, 1, 1)],
        vec![cell(1, 1, 0, 1)],
        vec![cell(0, 1, 1, 1)],
        vec![cell(1, 1, usize::MAX, 1)],
    ] {
        assert_eq!(
            render(document(cells, 10, 10)).unwrap_err(),
            CoreError::InvalidInput
        );
    }
    assert!(
        render(document(
            vec![cell(1, 1, 1, 2), cell(2, 1, 1, 2), cell(1, 3, 2, 1)],
            2,
            3
        ))
        .is_ok()
    );
}
#[test]
fn origin_cell_ceiling_precedes_event_allocations() {
    let cells = vec![cell(1, 1, 1, 1); 100001];
    assert_eq!(
        render(document(cells, 1, 1)).unwrap_err(),
        CoreError::Budget
    );
}
proptest! {
    #[test]
    fn sweep_matches_small_rectangle_oracle(rectangles in prop::collection::vec((1usize..20,1usize..20,1usize..5,1usize..5),0..30)) {
        let overlaps=rectangles.iter().enumerate().any(|(i,&(r,c,rs,cs))|rectangles.iter().skip(i+1).any(|&(r2,c2,rs2,cs2)|r<r2+rs2 && r2<r+rs && c<c2+cs2 && c2<c+cs));
        let cells=rectangles.iter().map(|&(r,c,rs,cs)|cell(r,c,rs,cs)).collect();
        let result=render(document(cells,30,30));
        prop_assert_eq!(result.is_ok(),!overlaps);
    }
}
