use lumen_docintel_core::{
    CoreError,
    canonical::render,
    formats::{common::Limits, ixbrl, json, jsonl, xbrl, xml},
};
use proptest::prelude::*;

#[test]
fn records_paths_and_security() {
    let d = json::parse(br#"{"z":1,"a":{"a/b~c":"two"}}"#, Limits::default()).unwrap();
    assert!(d.blocks[0].text.starts_with("/z = 1"));
    assert!(d.blocks[0].text.contains("/a/a~1b~0c = two"));
    let d = jsonl::parse(b"{\"x\":1}\n\n{\"x\":2}\n", Limits::default()).unwrap();
    assert_eq!(d.blocks.len(), 2);
    assert_eq!(
        d.blocks[1].regions[0].name.as_deref(),
        Some("record:2;line:3")
    );
    let deep = format!("{}0{}", "[".repeat(70), "]".repeat(70));
    assert_eq!(
        json::parse(deep.as_bytes(), Limits::default()),
        Err(CoreError::Budget)
    );
    let xml_deep = format!("{}x{}", "<r>".repeat(70), "</r>".repeat(70));
    assert_eq!(
        xml::parse(xml_deep.as_bytes(), Limits::default()),
        Err(CoreError::Budget)
    );
    for input in [
        b"<!DOCTYPE r SYSTEM 'file:///secret'><r/>".as_slice(),
        b"<!DOCTYPE r [<!ENTITY a 'x'><!ENTITY b '&a;&a;'>]><r>&b;</r>".as_slice(),
    ] {
        assert_eq!(
            xml::parse(input, Limits::default()),
            Err(CoreError::Unsupported)
        );
    }
    assert_eq!(
        xml::parse(b"<r>&unknown;</r>", Limits::default()),
        Err(CoreError::Parse)
    );
    assert_eq!(
        xml::parse(b"<r/><s/>", Limits::default()),
        Err(CoreError::Parse)
    );
    let mut l = Limits::default();
    l.budget.max_output_chars = 2;
    assert_eq!(json::parse(b"[12345]", l), Err(CoreError::Budget));
    let mut l = Limits::default();
    l.budget.max_memory_bytes = 128;
    assert_eq!(xml::parse(b"<r>x</r>", l), Err(CoreError::Budget));
}

#[test]
fn predefined_entities_preserve_adjacent_text() {
    let d = xml::parse(b"<r>H&amp;M<![CDATA[ company]]></r>", Limits::default()).unwrap();
    assert_eq!(d.blocks.len(), 1);
    assert_eq!(d.blocks[0].text, "/r[1] = H&M company");
}
#[test]
fn namespaces_facts_and_late_contexts() {
    assert_eq!(
        xbrl::parse(b"<r contextRef='C'>1</r>", Limits::default()),
        Err(CoreError::Unsupported)
    );
    assert_eq!(
        ixbrl::parse(b"<html contextRef='C'>1</html>", Limits::default()),
        Err(CoreError::Unsupported)
    );
    let bytes=br#"<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:c="urn:concept"><c:Mass contextRef="C" unitRef="U">-120</c:Mass><x:context id="C"><x:period><x:instant>2026-01-01</x:instant></x:period></x:context><x:unit id="U"><x:measure>kg</x:measure></x:unit></x:xbrl>"#;
    let d = xbrl::parse(bytes, Limits::default()).unwrap();
    let f = &d.generation.diagnostics.as_ref().unwrap()["facts"][0];
    assert_eq!(f["concept"], "{urn:concept}Mass");
    assert_eq!(f["value"], "-120");
    assert_eq!(f["unit"][0], "kg");
    assert_eq!(f["period"]["instant"], "2026-01-01");
    assert!(
        f["path"]
            .as_str()
            .unwrap()
            .ends_with("/{urn:concept}Mass[1]")
    );
    let inline=br#"<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" xmlns:c="urn:concept"><body><i:nonFraction name="c:Mass" contextRef="C" unitRef="U" scale="3">-<b>120</b></i:nonFraction><script>evil</script></body></html>"#;
    let d = ixbrl::parse(inline, Limits::default()).unwrap();
    let f = &d.generation.diagnostics.as_ref().unwrap()["facts"][0];
    assert_eq!(f["value"], "-120");
    assert_eq!(f["concept"], "{urn:concept}Mass");
    assert!(f["unit"].is_null());
    assert_eq!(f["attributes"]["scale"], "3");
    assert!(!d.blocks.iter().any(|b| b.text.contains("evil")));
    let forever=br#"<xbrl xmlns="http://www.xbrl.org/2003/instance"><context id="C"><period><forever/></period></context><n contextRef="C">1</n></xbrl>"#;
    let d = xbrl::parse(forever, Limits::default()).unwrap();
    assert_eq!(
        d.generation.diagnostics.as_ref().unwrap()["facts"][0]["period"]["forever"],
        true
    );
}

#[test]
fn inline_exclusion_is_explicitly_incomplete() {
    let source=br#"<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><ix:nonFraction name="Mass" contextRef="C">120<ix:exclude>annotation</ix:exclude></ix:nonFraction></html>"#;
    let d = ixbrl::parse(source, Limits::default()).unwrap();
    assert_eq!(d.generation.outcome.as_deref(), Some("partial"));
    assert!(
        !d.generation.diagnostics.as_ref().unwrap()["incomplete"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
proptest! {
 #[test]
 fn tagged_fact_unicode_locations(value in "[a-zé東京🦀]{1,30}") {
  let source=format!("<html xmlns:i=\"http://www.xbrl.org/2013/inlineXBRL\" xmlns:c=\"urn:concept\"><i:nonNumeric name=\"c:Label\" contextRef=\"C\">{value}</i:nonNumeric></html>");
  let r=render(ixbrl::parse(source.as_bytes(),Limits::default()).unwrap()).unwrap();
  let f=&r.document.generation.diagnostics.as_ref().unwrap()["facts"][0];
  let b=r.document.blocks.iter().find(|b|b.id==f["block_id"].as_str().unwrap()).unwrap();
  let start=f["value_start"].as_u64().unwrap() as usize;let end=f["value_end"].as_u64().unwrap() as usize;
  prop_assert_eq!(b.text.chars().skip(start).take(end-start).collect::<String>(),value);
  let bs=f["byte_start"].as_u64().unwrap() as usize;let be=f["byte_end"].as_u64().unwrap() as usize;
  prop_assert!(source[bs..be].starts_with("<i:nonNumeric"));
  prop_assert!(source[bs..be].ends_with("</i:nonNumeric>"));
  prop_assert_eq!(f["path"].as_str(),Some("/html[1]/{http://www.xbrl.org/2013/inlineXBRL}nonNumeric[1]"));
 }
 #[test]
 fn unicode_values_paths_and_offsets(values in prop::collection::vec("[a-zé東京🦀]{1,15}",1..15)) {
  let input=serde_json::to_vec(&values).unwrap();
  let r=render(json::parse(&input,Limits::default()).unwrap()).unwrap();
  let fields=r.document.generation.diagnostics.as_ref().unwrap()["records"][0]["fields"].as_array().unwrap();
  for (index,f) in fields.iter().enumerate() {
   let path=format!("/{index}");prop_assert_eq!(f["path"].as_str(),Some(path.as_str()));
   let start=f["value_start"].as_u64().unwrap() as usize;let end=f["value_end"].as_u64().unwrap() as usize;
   prop_assert_eq!(r.document.blocks[0].text.chars().skip(start).take(end-start).collect::<String>(),values[index].clone());
  }
  for (b,span) in r.document.blocks.iter().zip(&r.spans) {prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),b.text.clone());}
 }
 #[test]
 fn xml_sibling_locations(values in prop::collection::vec("[a-zé東京🦀]{1,15}",1..15)) {
  let source=format!("<r>\n{}\n</r>",values.iter().map(|v|format!("<n>{v}</n>")).collect::<Vec<_>>().join("\n"));
  let r=render(xml::parse(source.as_bytes(),Limits::default()).unwrap()).unwrap();
  for (i,value) in values.iter().enumerate() {
   let b=&r.document.blocks[i];
   prop_assert!(b.text.ends_with(value));
   let expected=format!("/r[1]/n[{}];line:{}",i+1,i+2);
   prop_assert_eq!(b.regions[0].name.as_deref(),Some(expected.as_str()));
  }
 }
}

#[test]
fn streamed_records_and_xml_stay_bounded() {
    let lines = (0..400)
        .map(|i| format!("{{\"n\":{i}}}\n"))
        .collect::<String>();
    let source = format!(
        "<r>{}</r>",
        (0..400).map(|i| format!("<n>{i}</n>")).collect::<String>()
    );
    let mut l = Limits::default();
    l.budget.max_memory_bytes = 16 * 1024 * 1024;
    assert_eq!(jsonl::parse(lines.as_bytes(), l).unwrap().blocks.len(), 400);
    assert_eq!(xml::parse(source.as_bytes(), l).unwrap().blocks.len(), 400);
    l.budget.max_memory_bytes = 64 * 1024;
    assert_eq!(jsonl::parse(lines.as_bytes(), l), Err(CoreError::Budget));
    assert_eq!(xml::parse(source.as_bytes(), l), Err(CoreError::Budget));
}

#[test]
fn json_numbers_preserve_precision_and_python_safe_text() {
    let source = br#"{"integer":18446744073709551617,"decimal":0.12345678901234567890123456789}"#;
    let d = json::parse(source, Limits::default()).unwrap();
    assert!(d.blocks[0].text.contains("18446744073709551617"));
    assert!(d.blocks[0].text.contains("0.12345678901234567890123456789"));
    let fields = &d.generation.diagnostics.as_ref().unwrap()["records"][0]["fields"];
    assert_eq!(fields[0]["number_text"], "18446744073709551617");
    assert_eq!(fields[1]["number_text"], "0.12345678901234567890123456789");
    assert_eq!(fields[0]["value"].to_string(), "18446744073709551617");
    let wire = serde_json::to_string(&d).unwrap();
    assert!(
        lumen_docintel_core::canonical::render_json(&wire)
            .unwrap()
            .contains("18446744073709551617")
    );
}
proptest! {
 #[test]
 fn arbitrary_integer_value_offsets(value in any::<u128>()) {
  let spelling=value.to_string();
  let source=format!("{{\"n\":{spelling}}}");
  let d=json::parse(source.as_bytes(),Limits::default()).unwrap();
  let field=&d.generation.diagnostics.as_ref().unwrap()["records"][0]["fields"][0];
  prop_assert_eq!(field["value"].to_string(),spelling.clone());
  prop_assert_eq!(field["number_text"].as_str(),Some(spelling.as_str()));
  let start=field["value_start"].as_u64().unwrap() as usize;
  let end=field["value_end"].as_u64().unwrap() as usize;
  prop_assert_eq!(d.blocks[0].text.chars().skip(start).take(end-start).collect::<String>(),spelling);
 }
}
