//! Bounded image-to-single-page-PDF conversion. Isolated worker only.
use crate::{CoreError, runtime::Context};
use flate2::{Compression, write::ZlibEncoder};
use image::{ImageFormat, ImageReader, Limits};
use std::io::{Cursor, Write};

pub fn wrap(bytes: &[u8], ctx: &Context) -> Result<String, CoreError> {
    ctx.input(bytes.len())?;
    let reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| CoreError::Parse)?;
    if !matches!(
        reader.format(),
        Some(ImageFormat::Png | ImageFormat::Jpeg | ImageFormat::WebP | ImageFormat::Tiff)
    ) {
        return Err(CoreError::Unsupported);
    }
    if reader.format() == Some(ImageFormat::Tiff) {
        let decoder =
            tiff::decoder::Decoder::new(Cursor::new(bytes)).map_err(|_| CoreError::Parse)?;
        if decoder.more_images() {
            return Err(CoreError::Unsupported);
        }
    }
    let (width, height) = reader.into_dimensions().map_err(|_| CoreError::Parse)?;
    let pixels = (width as usize)
        .checked_mul(height as usize)
        .ok_or(CoreError::Budget)?;
    if width == 0 || height == 0 || width > 16384 || height > 16384 {
        return Err(CoreError::Budget);
    }
    let allocation = pixels
        .checked_mul(24)
        .and_then(|v| v.checked_add(bytes.len() * 4 + 4 * 1024 * 1024))
        .ok_or(CoreError::Budget)?;
    let _memory = ctx.reserve(allocation)?;
    ctx.work(pixels.div_ceil(1024))?;
    let mut limits = Limits::default();
    limits.max_image_width = Some(16384);
    limits.max_image_height = Some(16384);
    limits.max_alloc = Some(allocation as u64);
    let mut reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| CoreError::Parse)?;
    reader.limits(limits);
    let decoded = reader.decode().map_err(|_| CoreError::Parse)?.into_rgba8();
    let mut rgb = Vec::with_capacity(pixels * 3);
    for pixel in decoded.pixels() {
        for value in &pixel.0[..3] {
            rgb.push(
                ((*value as u32 * pixel.0[3] as u32 + 255 * (255 - pixel.0[3] as u32) + 127) / 255)
                    as u8,
            );
        }
    }
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(&rgb).map_err(|_| CoreError::Internal)?;
    let compressed = encoder.finish().map_err(|_| CoreError::Internal)?;
    ctx.checkpoint()?;
    let content = format!("q {width} 0 0 {height} 0 0 cm /Im0 Do Q");
    let mut output = b"%PDF-1.4\n".to_vec();
    let mut offsets = vec![0];
    let objects=[
        b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_vec(),
        format!("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>").into_bytes(),
        [format!("<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {} >>\nstream\n",compressed.len()).into_bytes(),compressed,b"\nendstream".to_vec()].concat(),
        format!("<< /Length {} >>\nstream\n{content}\nendstream",content.len()).into_bytes()
    ];
    for (i, object) in objects.into_iter().enumerate() {
        offsets.push(output.len());
        output.extend(format!("{} 0 obj\n", i + 1).as_bytes());
        output.extend(object);
        output.extend(b"\nendobj\n");
    }
    let xref = output.len();
    output.extend(b"xref\n0 6\n0000000000 65535 f \n");
    for offset in offsets.iter().skip(1) {
        output.extend(format!("{offset:010} 00000 n \n").as_bytes());
    }
    output.extend(
        format!("trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").as_bytes(),
    );
    ctx.output(output.len())?;
    ctx.checkpoint()?;
    let hex = output
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect::<String>();
    serde_json::to_string(&serde_json::json!({"page":1,"pdf_hex":hex}))
        .map_err(|_| CoreError::Internal)
}
