//! Small arena view over the permissive HTML tree builder; no CSS engine.
use super::common::Session;
use crate::CoreError;
use html5ever::tendril::TendrilSink;
use markup5ever_rcdom::{Handle, NodeData, RcDom};
use std::collections::BTreeMap;

pub struct Element {
    name: String,
    attrs: BTreeMap<String, String>,
}
impl Element {
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs.get(name).map(String::as_str)
    }
}
pub struct Text {
    pub text: String,
}
pub enum Node {
    Element(Element),
    Text(Text),
    Other,
}
struct Entry {
    node: Node,
    children: Vec<usize>,
}
pub struct Html {
    nodes: Vec<Entry>,
    root: usize,
}
#[derive(Clone, Copy)]
pub struct ElementRef<'a> {
    dom: &'a Html,
    index: usize,
}
#[derive(Clone, Copy)]
pub struct NodeRef<'a> {
    dom: &'a Html,
    index: usize,
}
impl<'a> NodeRef<'a> {
    pub fn value(self) -> &'a Node {
        &self.dom.nodes[self.index].node
    }
}
pub struct Selector(String);
impl Selector {
    pub fn parse(value: &str) -> Result<Self, ()> {
        if value.bytes().all(|b| b.is_ascii_alphabetic()) {
            Ok(Self(value.into()))
        } else {
            Err(())
        }
    }
}
impl Html {
    pub fn parse_document(source: &str, s: &mut Session) -> Result<Self, CoreError> {
        let dom = html5ever::parse_document(RcDom::default(), Default::default()).one(source);
        let mut result = Self {
            nodes: vec![],
            root: 0,
        };
        fn copy(
            handle: &Handle,
            out: &mut Html,
            s: &mut Session,
            depth: usize,
        ) -> Result<usize, CoreError> {
            s.ctx.work(1)?;
            if depth > s.limits.max_depth + 2 {
                return Err(CoreError::Budget);
            }
            s.reserve(2048)?;
            let node = match &handle.data {
                NodeData::Element { name, attrs, .. } => Node::Element(Element {
                    name: name.local.to_string(),
                    attrs: attrs
                        .borrow()
                        .iter()
                        .map(|a| (a.name.local.to_string(), a.value.to_string()))
                        .collect(),
                }),
                NodeData::Text { contents } => Node::Text(Text {
                    text: contents.borrow().to_string(),
                }),
                _ => Node::Other,
            };
            let index = out.nodes.len();
            out.nodes.push(Entry {
                node,
                children: vec![],
            });
            for child in handle.children.borrow().iter() {
                let n = copy(child, out, s, depth + 1)?;
                out.nodes[index].children.push(n);
            }
            Ok(index)
        }
        copy(&dom.document, &mut result, s, 0)?;
        result.root = result
            .nodes
            .iter()
            .position(|entry| matches!(&entry.node,Node::Element(e) if e.name=="html"))
            .ok_or(CoreError::Parse)?;
        Ok(result)
    }
    pub fn root_element(&self) -> ElementRef<'_> {
        ElementRef {
            dom: self,
            index: self.root,
        }
    }
    pub fn select<'a>(&'a self, selector: &Selector) -> Select<'a> {
        self.root_element().select(selector)
    }
}
impl<'a> ElementRef<'a> {
    pub fn wrap(node: NodeRef<'a>) -> Option<Self> {
        matches!(node.value(), Node::Element(_)).then_some(Self {
            dom: node.dom,
            index: node.index,
        })
    }
    pub fn value(self) -> &'a Element {
        match &self.dom.nodes[self.index].node {
            Node::Element(e) => e,
            _ => unreachable!("element handle"),
        }
    }
    pub fn children(self) -> impl Iterator<Item = NodeRef<'a>> {
        self.dom.nodes[self.index]
            .children
            .iter()
            .map(move |&index| NodeRef {
                dom: self.dom,
                index,
            })
    }
    pub fn child_elements(self) -> impl Iterator<Item = Self> {
        self.children().filter_map(Self::wrap)
    }
    pub fn select(self, selector: &Selector) -> Select<'a> {
        Select {
            dom: self.dom,
            stack: self.dom.nodes[self.index]
                .children
                .iter()
                .rev()
                .copied()
                .collect(),
            name: selector.0.clone(),
        }
    }
    pub fn text(self) -> Texts<'a> {
        Texts {
            dom: self.dom,
            stack: self.dom.nodes[self.index]
                .children
                .iter()
                .rev()
                .copied()
                .collect(),
        }
    }
}
pub struct Select<'a> {
    dom: &'a Html,
    stack: Vec<usize>,
    name: String,
}
impl<'a> Iterator for Select<'a> {
    type Item = ElementRef<'a>;
    fn next(&mut self) -> Option<Self::Item> {
        while let Some(index) = self.stack.pop() {
            self.stack
                .extend(self.dom.nodes[index].children.iter().rev().copied());
            if let Node::Element(e) = &self.dom.nodes[index].node
                && e.name == self.name
            {
                return Some(ElementRef {
                    dom: self.dom,
                    index,
                });
            }
        }
        None
    }
}
pub struct Texts<'a> {
    dom: &'a Html,
    stack: Vec<usize>,
}
impl<'a> Iterator for Texts<'a> {
    type Item = &'a str;
    fn next(&mut self) -> Option<Self::Item> {
        while let Some(index) = self.stack.pop() {
            self.stack
                .extend(self.dom.nodes[index].children.iter().rev().copied());
            if let Node::Text(t) = &self.dom.nodes[index].node {
                return Some(&t.text);
            }
        }
        None
    }
}
