use lumen_docintel_core::{
    CoreError,
    canonical::{BlockKind, render},
    formats::{common::Limits, html::parse},
};
use proptest::prelude::*;
#[test]
fn main_tables_links_metadata_and_inert_scripts() {
    let bytes=br#"<!doctype html><html><head><title>Report</title><meta name="date" content="2026-10-08"></head><body><nav>drop navigation</nav><main><h1>Intro</h1><p>Read <a href="https://example.invalid">evidence</a></p><div class="cookie-banner"><table><tr><th>Mass</th><th>Unit</th></tr><tr><td>-120</td><td>kg</td></tr></table></div><script>panic('never')</script><p>PageTwo</p></main><footer>drop footer</footer></body></html>"#;
    let doc = parse(bytes, Limits::default()).unwrap();
    let d = doc.generation.diagnostics.as_ref().unwrap();
    assert_eq!(d["title"], "Report");
    assert_eq!(d["dates"][0], "2026-10-08");
    assert_eq!(d["links"][0]["target"], "https://example.invalid");
    assert!(
        doc.blocks
            .iter()
            .any(|b| b.kind == BlockKind::Table && b.text.contains("-120"))
    );
    let r = render(doc).unwrap();
    assert!(!r.rendered_text.contains("panic"));
    assert!(!r.rendered_text.contains("drop"));
    assert_eq!(
        r.document.blocks[0].regions[0].name.as_deref(),
        Some("/html[1]/body[1]/main[1]/h1[1]")
    );
}
#[test]
fn mhtml_and_budget_negatives() {
    let bytes=b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x\r\n\r\n--x\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n<h1>caf=C3=A9</h1><p>fact</p>\r\n--x--\r\n";
    assert!(
        render(parse(bytes, Limits::default()).unwrap())
            .unwrap()
            .rendered_text
            .contains("café")
    );
    let deep = format!("{}fact{}", "<div>".repeat(100), "</div>".repeat(100));
    assert_eq!(
        parse(deep.as_bytes(), Limits::default()),
        Err(CoreError::Budget)
    );
    let mut limits = Limits::default();
    limits.budget.max_output_chars = 1;
    assert_eq!(parse(b"<p>long</p>", limits), Err(CoreError::Budget));
    let mut limits = Limits::default();
    limits.budget.max_memory_bytes = 512;
    assert_eq!(parse(b"<p>long</p>", limits), Err(CoreError::Budget));
}
proptest! {
 #[test]
 fn table_cell_locations(rows in 1usize..20, columns in 1usize..8) {
  let mut source=String::from("<main><table>");
  for r in 1..=rows {source.push_str("<tr>");for c in 1..=columns {source.push_str(&format!("<td>r{r}c{c}</td>"));}source.push_str("</tr>");}
  source.push_str("</table></main>");
  let doc=parse(source.as_bytes(),Limits::default()).unwrap();
  let cells=&doc.blocks[0].table.as_ref().unwrap().cells;
  for cell in cells {
   prop_assert_eq!(&cell.text,&format!("r{}c{}",cell.row,cell.column));
   let path=format!("/html[1]/body[1]/main[1]/table[1]/tbody[1]/tr[{}]/td[{}]",cell.row,cell.column);
   prop_assert_eq!(cell.regions[0].name.as_deref(),Some(path.as_str()));
   let location=cell.regions[0].cell_range.as_ref().unwrap();
   prop_assert_eq!(location.row_start,cell.row);prop_assert_eq!(location.column_start,cell.column);
  }
 }
 #[test]
 fn unicode_offsets_and_element_paths(values in prop::collection::vec("[a-zé東京🦀]{1,20}",1..20)) {
  let source=format!("<main>{}</main>",values.iter().map(|v|format!("<p>{v}</p>")).collect::<String>());
  let r=render(parse(source.as_bytes(),Limits::default()).unwrap()).unwrap();
  for (n,(span,block)) in r.spans.iter().zip(&r.document.blocks).enumerate() {
   prop_assert_eq!(&block.text,&values[n]);
   let path=format!("/html[1]/body[1]/main[1]/p[{}]",n+1);
   prop_assert_eq!(block.regions[0].name.as_deref(),Some(path.as_str()));
   prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),block.text.clone());
  }
 }
}
