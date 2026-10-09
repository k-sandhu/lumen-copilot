//! Well-formed inline facts and inert readable XML/XHTML content.
use super::common::Limits;
use crate::{CoreError, canonical::Document};
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    super::xml::parse_mode(bytes, limits, super::xml::Mode::Inline)
}
