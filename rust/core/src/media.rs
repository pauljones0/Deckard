use image::{ImageBuffer, Rgb, RgbImage};
use turbojpeg::{Compressor, PixelFormat, Subsamp};

/// Encode already-composited RGB pixels as 4:4:4 JPEG. Device orientation is
/// supplied explicitly, so cache keys and strip geometry keep the fork's rules.
pub fn encode_rgb(
    data: &[u8],
    width: u32,
    height: u32,
    rotation: u16,
    flips: (bool, bool),
    quality: u8,
) -> Result<Vec<u8>, String> {
    if width == 0 || height == 0 || width > u16::MAX as u32 || height > u16::MAX as u32 {
        return Err("invalid image dimensions".into());
    }
    if quality == 0 || quality > 100 {
        return Err("JPEG quality must be 1 to 100".into());
    }
    let expected = (width as usize)
        .checked_mul(height as usize)
        .and_then(|pixels| pixels.checked_mul(3))
        .ok_or("image dimensions overflow")?;
    if data.len() != expected {
        return Err("RGB buffer size does not match image dimensions".into());
    }
    if !rotation.is_multiple_of(90) {
        return Err("rotation must be a multiple of 90".into());
    }
    let oriented = if rotation.is_multiple_of(360) && !flips.0 && !flips.1 {
        None
    } else {
        let source: RgbImage = ImageBuffer::<Rgb<u8>, _>::from_raw(width, height, data.to_vec())
            .ok_or("invalid RGB buffer")?;
        let mut source = match rotation % 360 {
            90 => image::imageops::rotate270(&source),
            180 => image::imageops::rotate180(&source),
            270 => image::imageops::rotate90(&source),
            _ => source,
        };
        if flips.0 {
            image::imageops::flip_horizontal_in_place(&mut source)
        }
        if flips.1 {
            image::imageops::flip_vertical_in_place(&mut source)
        }
        Some(source)
    };
    let (pixels, width, height) = oriented
        .as_ref()
        .map(|i| (i.as_raw().as_slice(), i.width(), i.height()))
        .unwrap_or((data, width, height));
    let mut encoder = Compressor::new().map_err(|error| error.to_string())?;
    encoder
        .set_quality(quality.into())
        .map_err(|error| error.to_string())?;
    encoder
        .set_subsamp(Subsamp::None)
        .map_err(|error| error.to_string())?;
    encoder
        .compress_to_vec(turbojpeg::Image {
            pixels,
            width: width as usize,
            height: height as usize,
            pitch: width as usize * 3,
            format: PixelFormat::RGB,
        })
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_malformed_buffers_before_encoding() {
        assert!(encode_rgb(&[0; 2], 1, 1, 0, (false, false), 90).is_err());
        assert!(encode_rgb(&[], 0, 1, 0, (false, false), 90).is_err());
        assert!(encode_rgb(&[0; 3], 1, 1, 45, (false, false), 90).is_err());
    }
}
