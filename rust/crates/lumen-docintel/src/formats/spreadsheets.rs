use super::package::Limits;
use crate::{CoreError,canonical::Document};
pub fn extract(_bytes:&[u8],_limits:Limits)->Result<Document,CoreError> {Err(CoreError::Unsupported)}
