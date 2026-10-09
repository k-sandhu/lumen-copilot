//! Physical-line JSON records, with one bounded tree alive at a time.
use super::common::Limits;
use crate::{CoreError, canonical::Document};
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    super::json::parse_mode(bytes, limits, true)
}
