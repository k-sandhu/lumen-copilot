#![allow(dead_code)] // Shared generated-fixture helpers used by separate integration tests.
pub fn text(x: i32, y: i32, size: i32, value: &str) -> String {
    format!(
        "BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({}) Tj ET",
        value
            .replace('\\', "\\\\")
            .replace('(', "\\(")
            .replace(')', "\\)")
    )
}
pub fn objects(objects: &[Vec<u8>], trailer: &str) -> Vec<u8> {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = vec![0];
    for (i, obj) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n", i + 1).as_bytes());
        out.extend_from_slice(obj);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
    );
    for offset in &offsets[1..] {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R {trailer} >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    out
}
pub fn pdf_bytes(streams: &[&[u8]], rotation: i32) -> Vec<u8> {
    let kids = (0..streams.len())
        .map(|i| format!("{} 0 R", 4 + i * 2))
        .collect::<Vec<_>>()
        .join(" ");
    let mut objs = vec![
        b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
        format!("<< /Type /Pages /Kids [{kids}] /Count {} >>", streams.len()).into_bytes(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>".to_vec(),
    ];
    for (i, stream) in streams.iter().enumerate() {
        objs.push(format!("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate {rotation} /Resources << /Font << /F1 3 0 R >> >> /Contents {} 0 R >>",5+i*2).into_bytes());
        let mut obj = format!("<< /Length {} >>\nstream\n", stream.len()).into_bytes();
        obj.extend_from_slice(stream);
        obj.extend_from_slice(b"\nendstream");
        objs.push(obj);
    }
    objects(&objs, "")
}

pub fn unicode_pdf(value:&str)->Vec<u8> {
    let mut map=String::from("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapType 2 def 1 begincodespacerange <0000> <ffff> endcodespacerange\n");
    let mut encoded=String::new();
    let chars:Vec<_>=value.chars().collect();map.push_str(&format!("{} beginbfchar\n",chars.len()));
    for (i,c) in chars.iter().enumerate(){let mut units=[0u16;2];let units=c.encode_utf16(&mut units);let target=units.iter().map(|u|format!("{u:04x}")).collect::<String>();map.push_str(&format!("<{:04x}> <{target}>\n",i+1));encoded.push_str(&format!("{:04x}",i+1));}
    map.push_str("endbfchar endcmap end end");
    let content=format!("BT /F1 10 Tf 1 0 0 1 40 700 Tm <{encoded}> Tj ET");
    objects(&[b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>".to_vec(),b"<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >>".to_vec(),b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>".to_vec(),format!("<< /Length {} >>\nstream\n{content}\nendstream",content.len()).into_bytes(),b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Synthetic /DW 600 >>".to_vec(),format!("<< /Length {} >>\nstream\n{map}\nendstream",map.len()).into_bytes()],"")
}
