use crate::{CoreError, canonical::Document, runtime::Budget};

pub fn extract(_bytes: &[u8], _limits: Budget) -> Result<Document, CoreError> {
    Err(CoreError::Unsupported)
}
