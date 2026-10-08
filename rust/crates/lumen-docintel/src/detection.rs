//! Bounded content recognition. Recognition never enables an upload/parser.
use crate::CoreError;
use chardetng::{EncodingDetector, Iso2022JpDetection, Utf8Detection};
use encoding_rs::{DecoderResult, Encoding, UTF_8, UTF_16BE, UTF_16LE};
use serde::{Deserialize, Serialize};
use std::io::{Cursor, Read};

const MAX_INPUT: usize = 32 * 1024 * 1024;
const MAX_METADATA: u64 = 65_536;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Format {
    Pdf,
    Docx,
    Dotx,
    Docm,
    Xlsx,
    Xlsm,
    Pptx,
    Pptm,
    Odt,
    Ods,
    Odp,
    Epub,
    Zip,
    Tar,
    Gzip,
    Doc,
    Xls,
    Ppt,
    Msg,
    Rtf,
    Html,
    Xhtml,
    Mhtml,
    Text,
    Markdown,
    Csv,
    Tsv,
    Json,
    Jsonl,
    Xml,
    Xbrl,
    Ixbrl,
    Ipynb,
    Eml,
    Mbox,
    Image,
}

impl Format {
    pub fn mime(self) -> &'static str {
        match self {
            Self::Pdf => "application/pdf",
            Self::Docx => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            Self::Dotx => "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
            Self::Docm => "application/vnd.ms-word.document.macroenabled.12",
            Self::Xlsx => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            Self::Xlsm => "application/vnd.ms-excel.sheet.macroenabled.12",
            Self::Pptx => {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            }
            Self::Pptm => "application/vnd.ms-powerpoint.presentation.macroenabled.12",
            Self::Odt => "application/vnd.oasis.opendocument.text",
            Self::Ods => "application/vnd.oasis.opendocument.spreadsheet",
            Self::Odp => "application/vnd.oasis.opendocument.presentation",
            Self::Epub => "application/epub+zip",
            Self::Zip => "application/zip",
            Self::Tar => "application/x-tar",
            Self::Gzip => "application/gzip",
            Self::Doc => "application/msword",
            Self::Xls => "application/vnd.ms-excel",
            Self::Ppt => "application/vnd.ms-powerpoint",
            Self::Msg => "application/vnd.ms-outlook",
            Self::Rtf => "application/rtf",
            Self::Html => "text/html",
            Self::Xhtml => "application/xhtml+xml",
            Self::Mhtml => "multipart/related",
            Self::Text => "text/plain",
            Self::Markdown => "text/markdown",
            Self::Csv => "text/csv",
            Self::Tsv => "text/tab-separated-values",
            Self::Json => "application/json",
            Self::Jsonl => "application/x-ndjson",
            Self::Xml => "application/xml",
            Self::Xbrl => "application/xbrl+xml",
            Self::Ixbrl => "application/ixbrl+xml",
            Self::Ipynb => "application/x-ipynb+json",
            Self::Eml => "message/rfc822",
            Self::Mbox => "application/mbox",
            Self::Image => "image/*",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecodedText {
    pub text: String,
    pub encoding: String,
    pub evidence: String,
    pub errors: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Detection {
    pub format: Format,
    pub mime: String,
    pub declared_mime: Option<String>,
    pub declared_mismatch: bool,
    pub evidence: String,
    pub decoded: Option<DecodedText>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Route {
    Native,
    PythonFallback,
    Unsupported,
}

/// `landed` is maintained by parser registration, never by user configuration.
pub fn route(format: Format, landed: &[Format], enabled: &[Format]) -> Route {
    if landed.contains(&format) && enabled.contains(&format) {
        return Route::Native;
    }
    if matches!(
        format,
        Format::Pdf | Format::Docx | Format::Pptx | Format::Xlsx | Format::Text | Format::Markdown
    ) {
        Route::PythonFallback
    } else {
        Route::Unsupported
    }
}

pub fn decode_text(data: &[u8]) -> Result<DecodedText, CoreError> {
    if data.len() > MAX_INPUT {
        return Err(CoreError::Budget);
    }
    let (encoding, skip, evidence): (&'static Encoding, usize, &str) =
        if let Some((encoding, skip)) = Encoding::for_bom(data) {
            (encoding, skip, "bom")
        } else {
            let sample = &data[..data.len().min(65_536)];
            let even = sample.iter().step_by(2).filter(|&&b| b == 0).count();
            let odd = sample
                .iter()
                .skip(1)
                .step_by(2)
                .filter(|&&b| b == 0)
                .count();
            if sample.len() >= 8 && odd > sample.len() / 8 && even < odd / 4 {
                (UTF_16LE, 0, "heuristic_utf16")
            } else if sample.len() >= 8 && even > sample.len() / 8 && odd < even / 4 {
                (UTF_16BE, 0, "heuristic_utf16")
            } else if std::str::from_utf8(data).is_ok() {
                (UTF_8, 0, "valid_utf8")
            } else {
                let mut detector = EncodingDetector::new(Iso2022JpDetection::Deny);
                detector.feed(sample, true);
                (detector.guess(None, Utf8Detection::Allow), 0, "statistical")
            }
        };
    let mut decoder = encoding.new_decoder_without_bom_handling();
    let mut pos = skip;
    let mut text = String::new();
    let mut errors = 0;
    let mut chars = 0;
    let mut buffer = [0_u8; 4096];
    loop {
        let (result, read, written) =
            decoder.decode_to_utf8_without_replacement(&data[pos..], &mut buffer, true);
        pos += read;
        let part = std::str::from_utf8(&buffer[..written]).map_err(|_| CoreError::Internal)?;
        chars += part.chars().count();
        if chars > 2_000_000 {
            return Err(CoreError::Budget);
        }
        text.push_str(part);
        match result {
            DecoderResult::InputEmpty => break,
            DecoderResult::OutputFull => {}
            DecoderResult::Malformed(..) => {
                text.push('\u{fffd}');
                errors += 1;
                chars += 1;
            }
        }
        if chars > 2_000_000 {
            return Err(CoreError::Budget);
        }
    }
    Ok(DecodedText {
        text,
        encoding: encoding.name().into(),
        evidence: evidence.into(),
        errors,
    })
}

fn zip_tail(data: &[u8]) -> Option<usize> {
    let start = data.len().saturating_sub(65_557);
    (start..data.len().saturating_sub(21)).rev().find(|&n| {
        data.get(n..n + 4) == Some(b"PK\x05\x06".as_slice())
            && n + 22 + u16::from_le_bytes([data[n + 20], data[n + 21]]) as usize == data.len()
    })
}

fn metadata(
    zip: &mut zip::ZipArchive<Cursor<&[u8]>>,
    name: &str,
) -> Result<Option<String>, CoreError> {
    let mut file = match zip.by_name(name) {
        Ok(file) => file,
        Err(zip::result::ZipError::FileNotFound) => return Ok(None),
        Err(_) => return Err(CoreError::Parse),
    };
    if file.size() > MAX_METADATA {
        return Err(CoreError::Budget);
    }
    let mut bytes = vec![];
    (&mut file)
        .take(MAX_METADATA + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| CoreError::Parse)?;
    if bytes.len() as u64 > MAX_METADATA {
        return Err(CoreError::Budget);
    }
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| CoreError::Parse)
}

fn detect_zip(data: &[u8]) -> Result<Format, CoreError> {
    let tail = zip_tail(data).ok_or(CoreError::Parse)?;
    let count = u16::from_le_bytes([data[tail + 10], data[tail + 11]]) as usize;
    if count > 10_000 {
        return Err(CoreError::Budget);
    } // ZIP64 also fails closed here.
    let mut zip = zip::ZipArchive::new(Cursor::new(data)).map_err(|_| CoreError::Parse)?;
    if zip.len() > 10_000 {
        return Err(CoreError::Budget);
    }
    let names: Vec<&str> = zip.file_names().collect();
    let word = names.contains(&"word/document.xml");
    let sheet = names.contains(&"xl/workbook.xml");
    let slide = names.contains(&"ppt/presentation.xml");
    if usize::from(word) + usize::from(sheet) + usize::from(slide) > 1 {
        return Err(CoreError::Unsupported);
    }
    let mime = metadata(&mut zip, "mimetype")?;
    if mime.is_some() && (word || sheet || slide) {
        return Err(CoreError::Unsupported);
    }
    if let Some(mime) = mime {
        return match mime.trim() {
            "application/epub+zip" => Ok(Format::Epub),
            "application/vnd.oasis.opendocument.text" => Ok(Format::Odt),
            "application/vnd.oasis.opendocument.spreadsheet" => Ok(Format::Ods),
            "application/vnd.oasis.opendocument.presentation" => Ok(Format::Odp),
            _ => Ok(Format::Zip),
        };
    }
    let types = metadata(&mut zip, "[Content_Types].xml")?
        .unwrap_or_default()
        .to_ascii_lowercase();
    let macros = types.contains("macroenabled");
    if word {
        Ok(if macros {
            Format::Docm
        } else if types.contains("template.main+xml") {
            Format::Dotx
        } else {
            Format::Docx
        })
    } else if sheet {
        Ok(if macros { Format::Xlsm } else { Format::Xlsx })
    } else if slide {
        Ok(if macros { Format::Pptm } else { Format::Pptx })
    } else {
        Ok(Format::Zip)
    }
}

fn compound(data: &[u8]) -> Result<Format, CoreError> {
    let file = cfb::CompoundFile::open(Cursor::new(data)).map_err(|_| CoreError::Parse)?;
    let candidates: Vec<Format> = [
        ("/WordDocument", Format::Doc),
        ("/Workbook", Format::Xls),
        ("/Book", Format::Xls),
        ("/PowerPoint Document", Format::Ppt),
        ("/__properties_version1.0", Format::Msg),
    ]
    .into_iter()
    .filter_map(|(name, format)| file.is_stream(name).then_some(format))
    .collect();
    if candidates.len() == 1 {
        Ok(candidates[0])
    } else {
        Err(CoreError::Unsupported)
    }
}

fn delimited(text: &str) -> Option<Format> {
    for delimiter in ['\t', ',', ';', '|'] {
        let mut quoted = false;
        let mut count = 0;
        let mut rows = vec![];
        let mut chars = text.chars().peekable();
        while let Some(c) = chars.next() {
            if c == '"' {
                if quoted && chars.peek() == Some(&'"') {
                    chars.next();
                } else {
                    quoted = !quoted;
                }
            } else if !quoted && c == delimiter {
                count += 1;
            } else if !quoted && c == '\n' {
                rows.push(count);
                count = 0;
                if rows.len() == 10 {
                    break;
                }
            }
        }
        if count > 0 {
            rows.push(count);
        }
        if !quoted && rows.len() >= 2 && rows[0] > 0 && rows.iter().all(|n| *n == rows[0]) {
            return Some(if delimiter == '\t' {
                Format::Tsv
            } else {
                Format::Csv
            });
        }
    }
    None
}

fn text_format(text: &str) -> Format {
    let trimmed = text.trim_start();
    let prefix: String = trimmed.chars().take(65_536).collect();
    let lower = prefix.to_ascii_lowercase();
    if lower.starts_with("{\\rtf") {
        return Format::Rtf;
    }
    if trimmed.starts_with("From ") && lower.contains("\nfrom:") {
        return Format::Mbox;
    }
    if lower.contains("content-type: multipart/related") && lower.contains("mime-version:") {
        return Format::Mhtml;
    }
    let headers = lower.split("\n\n").next().unwrap_or("");
    if (headers.starts_with("from:")
        || headers.starts_with("subject:")
        || headers.starts_with("mime-version:"))
        && headers.contains("\n")
        && headers.contains(':')
    {
        return Format::Eml;
    }
    if lower.starts_with('<') {
        if lower.contains("<ix:") || lower.contains("xmlns:ix=") {
            return Format::Ixbrl;
        }
        if lower.contains("<xbrl") || lower.contains("<xbrli:xbrl") {
            return Format::Xbrl;
        }
        if lower.contains("http://www.w3.org/1999/xhtml") {
            return Format::Xhtml;
        }
        if lower.starts_with("<!doctype html") || lower.starts_with("<html") {
            return Format::Html;
        }
        return Format::Xml;
    }
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(text) {
        return if value.get("nbformat").is_some()
            && value.get("cells").is_some_and(|v| v.is_array())
        {
            Format::Ipynb
        } else {
            Format::Json
        };
    }
    let lines: Vec<&str> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .take(10)
        .collect();
    if lines.len() > 1
        && lines
            .iter()
            .all(|l| serde_json::from_str::<serde_json::Value>(l).is_ok())
    {
        return Format::Jsonl;
    }
    if let Some(format) = delimited(&prefix) {
        return format;
    }
    if trimmed.starts_with("# ") || trimmed.starts_with("```") || trimmed.starts_with("## ") {
        Format::Markdown
    } else {
        Format::Text
    }
}

pub fn detect(data: &[u8], declared: Option<&str>) -> Result<Detection, CoreError> {
    if data.len() > MAX_INPUT {
        return Err(CoreError::Budget);
    }
    let mut decoded = None;
    let (format, mut mime, evidence) = if data.starts_with(b"%PDF-") {
        if zip_tail(data).is_some() {
            return Err(CoreError::Unsupported);
        }
        (Format::Pdf, Format::Pdf.mime().into(), "magic")
    } else if data.starts_with(b"PK\x03\x04") || data.starts_with(b"PK\x05\x06") {
        let format = detect_zip(data)?;
        (format, format.mime().into(), "container")
    } else if data.starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") {
        let format = compound(data)?;
        (format, format.mime().into(), "container")
    } else if data.starts_with(b"\x1f\x8b") {
        (Format::Gzip, Format::Gzip.mime().into(), "magic")
    } else if data.get(257..262) == Some(b"ustar".as_slice()) {
        (Format::Tar, Format::Tar.mime().into(), "magic")
    } else if let Some(kind) = infer::get(data).filter(|k| k.mime_type().starts_with("image/")) {
        (Format::Image, kind.mime_type().into(), "magic")
    } else {
        let text = decode_text(data)?;
        let total = text.text.chars().count();
        let controls = text
            .text
            .chars()
            .filter(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
            .count();
        if total > 0 && controls.saturating_mul(100) > total {
            return Err(CoreError::Unsupported);
        }
        let format = text_format(&text.text);
        decoded = Some(text);
        (format, format.mime().into(), "text_heuristic")
    };
    mime.make_ascii_lowercase();
    let declared_mime = declared.map(|s| {
        s.split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase()
    });
    let declared_mismatch = declared_mime.as_ref().is_some_and(|s| s != &mime);
    Ok(Detection {
        format,
        mime,
        declared_mime,
        declared_mismatch,
        evidence: evidence.into(),
        decoded,
    })
}

pub fn detect_json(data: &[u8], declared: Option<&str>) -> Result<String, CoreError> {
    serde_json::to_string(&detect(data, declared)?).map_err(|_| CoreError::Internal)
}
