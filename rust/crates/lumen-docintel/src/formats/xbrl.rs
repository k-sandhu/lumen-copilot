//! Instance facts with supplied context and unit metadata.
use super::common::Limits;
use crate::{CoreError, canonical::Document};
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    super::xml::parse_mode(bytes, limits, super::xml::Mode::Xbrl)
}
