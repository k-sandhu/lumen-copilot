//! Conservative lexical preflight before serde's bounded JSON tree allocation.
use super::common::Session;
use crate::CoreError;
use serde_json::Value;

pub fn read(bytes: &[u8], s: &mut Session) -> Result<Value, CoreError> {
    let mut depth = 0usize;
    let mut quoted = false;
    let mut escaped = false;
    let mut scalar = false;
    for (i, &b) in bytes.iter().enumerate() {
        if i % 1024 == 0 {
            s.ctx.checkpoint()?;
        }
        if quoted {
            if escaped {
                escaped = false;
            } else if b == b'\\' {
                escaped = true;
            } else if b == b'"' {
                quoted = false;
            }
            continue;
        }
        match b {
            b'"' => {
                s.ctx.work(1)?;
                s.reserve(256)?;
                quoted = true;
                scalar = false;
            }
            b'{' | b'[' => {
                s.ctx.work(1)?;
                s.reserve(256)?;
                depth += 1;
                if depth > s.limits.max_depth {
                    return Err(CoreError::Budget);
                }
                scalar = false;
            }
            b'}' | b']' => {
                depth = depth.checked_sub(1).ok_or(CoreError::Parse)?;
                scalar = false;
            }
            b',' | b':' | b' ' | b'\r' | b'\n' | b'\t' => scalar = false,
            _ => {
                if !scalar {
                    s.ctx.work(1)?;
                    s.reserve(256)?;
                    scalar = true;
                }
            }
        }
    }
    if quoted || depth != 0 {
        return Err(CoreError::Parse);
    }
    let mut decoder = serde_json::Deserializer::from_reader(bytes);
    let value = serde::Deserialize::deserialize(&mut decoder).map_err(|_| CoreError::Parse)?;
    decoder.end().map_err(|_| CoreError::Parse)?;
    s.ctx.checkpoint()?;
    Ok(value)
}
