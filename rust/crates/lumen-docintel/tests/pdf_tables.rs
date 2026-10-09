mod pdf_support;
use lumen_docintel_core::{canonical::{self,BlockKind,HeaderRole},formats::pdf,runtime::Budget};
use pdf_support::{pdf_bytes,text};
fn grid(xs:&[i32],ys:&[i32])->String {
    let mut lines=String::new();
    for x in xs {lines.push_str(&format!("{x} {} m {x} {} l S\n",ys[0],ys[ys.len()-1]));}
    for y in ys {lines.push_str(&format!("{} {y} m {} {y} l S\n",xs[0],xs[xs.len()-1]));}
    lines
}
fn body(y:i32,rows:&[(&str,&str)])->String{rows.iter().enumerate().map(|(i,(a,b))|format!("{}\n{}",text(50,y-i as i32*30,12,a),text(250,y-i as i32*30,12,b))).collect::<Vec<_>>().join("\n")}
#[test]
fn bordered_cells_headers_values_and_boxes(){
    let content=grid(&[40,240,440],&[740,710,680,650])+&body(720,&[("Region","Mass"),("North","-120 kg*"),("South","+30 kg")]);
    let doc=pdf::extract(&pdf_bytes(&[content.as_bytes()],0),Budget::default()).unwrap();
    let table=doc.blocks.iter().find_map(|b|b.table.as_ref()).expect("bordered table");
    assert_eq!((table.rows,table.columns),(3,2));assert_eq!(table.cells[0].header_role,HeaderRole::Column);
    assert!(table.cells.iter().any(|c|c.row==2&&c.column==2&&c.text=="-120 kg*"));
    assert!(table.cells.iter().all(|c|c.regions[0].number==Some(1)&&c.regions[0].bbox.is_some()));
    let r=canonical::render(doc).unwrap();assert!(r.rendered_text.contains("C2 [Mass]=-120 kg*"));
    for(s,b)in r.spans.iter().zip(&r.document.blocks){let slice:String=r.rendered_text.chars().skip(s.char_start).take(s.char_end-s.char_start).collect();assert_eq!(slice,b.text);}
}
#[test]
fn borderless_alignment_does_not_force_prose_columns_into_tables(){
    let data=body(700,&[("Region","Mass"),("North","-120 kg*"),("South","+30 kg")]);
    let doc=pdf::extract(&pdf_bytes(&[data.as_bytes()],0),Budget::default()).unwrap();
    assert_eq!(doc.blocks.iter().filter(|b|b.kind==BlockKind::Table).count(),1);
    for rows in [[("LeftOne","RightOne"),("LeftTwo","RightTwo"),("LeftThree","RightThree")],[("Chapter one","Other chapter"),("Long prose","More prose"),("Final words","Closing words")]] {
        let prose=body(700,&rows);let doc=pdf::extract(&pdf_bytes(&[prose.as_bytes()],0),Budget::default()).unwrap();
        assert!(doc.blocks.iter().all(|b|b.table.is_none()));
    }
}
#[test]
fn missing_inner_ruling_creates_spanning_origin_cell(){
    let content=grid(&[40,440],&[740,710,680])+"240 710 m 240 680 l S\n"+&text(50,720,12,"Shared heading")+"\n"+&body(690,&[("North","-120 kg")]);
    let doc=pdf::extract(&pdf_bytes(&[content.as_bytes()],0),Budget::default()).unwrap();
    let table=doc.blocks.iter().find_map(|b|b.table.as_ref()).unwrap();
    assert!(table.cells.iter().any(|c|c.row==1&&c.column==1&&c.column_span==2&&c.text=="Shared heading"));
}
#[test]
fn consecutive_page_tables_join_with_cell_provenance_and_exact_page_segments(){
    let first=grid(&[40,240,440],&[160,130,100])+&body(140,&[("Region","Mass"),("North","-120 kg")]);
    let second=grid(&[40,240,440],&[750,720,690])+&body(730,&[("Region","Mass"),("South","+30 kg")]);
    let doc=pdf::extract(&pdf_bytes(&[first.as_bytes(),second.as_bytes()],0),Budget::default()).unwrap();
    let tables:Vec<_>=doc.blocks.iter().filter_map(|b|b.table.as_ref()).collect();assert_eq!(tables.len(),1);
    assert_eq!(tables[0].rows,4);assert!(tables[0].cells.iter().any(|c|c.text=="+30 kg"&&c.regions[0].number==Some(2)));
    let rendered=canonical::render(doc).unwrap();let parts=&rendered.document.source_parts;assert_eq!(parts.len(),2);
    assert!(parts[0].char_end<=parts[1].char_start);
    let page1:String=rendered.rendered_text.chars().skip(parts[0].char_start).take(parts[0].char_end-parts[0].char_start).collect();let page2:String=rendered.rendered_text.chars().skip(parts[1].char_start).take(parts[1].char_end-parts[1].char_start).collect();assert!(page1.contains("North")&&!page1.contains("South"));assert!(page2.contains("South")&&!page2.contains("North"));
}
