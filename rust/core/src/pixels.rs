//! Exact channel packing and bounded metadata for shared image composition.
use image::RgbaImage;
use std::{
    mem::MaybeUninit,
    sync::{Arc, Weak},
};

/// Weak references prevent address reuse without retaining image pixel buffers.
#[derive(Default)]
pub(crate) struct OpacityCache(Vec<(Weak<RgbaImage>, bool)>);
impl OpacityCache {
    pub(crate) fn clear(&mut self) {
        self.0.clear();
    }
    pub(crate) fn opaque(&mut self, image: &Arc<RgbaImage>) -> bool {
        // Usually one shared source; a short list avoids hashing/allocation metadata.
        if let Some((_, opaque)) = self
            .0
            .iter()
            .find(|(source, _)| std::ptr::eq(source.as_ptr(), Arc::as_ptr(image)))
        {
            return *opaque;
        }
        // Unknown images keep the normal row compositor when the metadata bound is full.
        if self.0.len() >= 64 {
            return false;
        }
        let opaque = image.pixels().all(|pixel| pixel.0[3] == 255);
        self.0.push((Arc::downgrade(image), opaque));
        opaque
    }
}

pub(crate) fn rgb_pixels(image: &RgbaImage) -> Arc<[u8]> {
    let pixels = image.width() as usize * image.height() as usize;
    let source = &image.as_raw()[..pixels * 4];
    let mut output = Arc::<[u8]>::new_uninit_slice(pixels * 3);
    pack_rgb(
        source,
        Arc::get_mut(&mut output).expect("unshared RGB buffer"),
    );
    // SAFETY: pack_rgb writes every destination byte, including all short tails.
    unsafe { output.assume_init() }
}

fn pack_rgb(source: &[u8], output: &mut [MaybeUninit<u8>]) {
    assert!(source.len().is_multiple_of(4));
    assert_eq!(output.len(), source.len() / 4 * 3);
    #[cfg(target_arch = "x86_64")]
    if source.len() >= 64 && std::is_x86_feature_detected!("ssse3") {
        // SAFETY: runtime detection establishes SSSE3; lengths were validated above.
        unsafe {
            pack_ssse3(source, output);
        }
        return;
    }
    #[cfg(target_arch = "aarch64")]
    if source.len() >= 64 && std::arch::is_aarch64_feature_detected!("neon") {
        // SAFETY: runtime detection establishes NEON; lengths were validated above.
        unsafe {
            pack_neon(source, output);
        }
        return;
    }
    pack_scalar(source, output);
}

fn pack_scalar(source: &[u8], output: &mut [MaybeUninit<u8>]) {
    for (rgba, rgb) in source
        .as_chunks::<4>()
        .0
        .iter()
        .zip(output.as_chunks_mut::<3>().0)
    {
        for channel in 0..3 {
            rgb[channel].write(rgba[channel]);
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "ssse3")]
unsafe fn pack_ssse3(source: &[u8], output: &mut [MaybeUninit<u8>]) {
    use std::arch::x86_64::*;
    let shuffle = _mm_setr_epi8(
        0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, -128, -128, -128, -128,
    );
    let (blocks, tail) = source.as_chunks::<64>();
    let (targets, target_tail) = output.as_chunks_mut::<48>();
    for (source, target) in blocks.iter().zip(targets) {
        // SAFETY: each source is exactly 64 readable bytes and each target has
        // exactly 48 writable bytes. Unaligned loads/stores accept arbitrary offsets.
        unsafe {
            let input = source.as_ptr().cast();
            let a = _mm_shuffle_epi8(_mm_loadu_si128(input), shuffle);
            let b = _mm_shuffle_epi8(_mm_loadu_si128(input.add(1)), shuffle);
            let c = _mm_shuffle_epi8(_mm_loadu_si128(input.add(2)), shuffle);
            let d = _mm_shuffle_epi8(_mm_loadu_si128(input.add(3)), shuffle);
            let output = target.as_mut_ptr().cast();
            _mm_storeu_si128(output, _mm_or_si128(a, _mm_slli_si128::<12>(b)));
            _mm_storeu_si128(
                output.add(1),
                _mm_or_si128(_mm_srli_si128::<4>(b), _mm_slli_si128::<8>(c)),
            );
            _mm_storeu_si128(
                output.add(2),
                _mm_or_si128(_mm_srli_si128::<8>(c), _mm_slli_si128::<4>(d)),
            );
        }
    }
    pack_scalar(tail, target_tail);
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn pack_neon(source: &[u8], output: &mut [MaybeUninit<u8>]) {
    use std::arch::aarch64::*;
    let (blocks, tail) = source.as_chunks::<64>();
    let (targets, target_tail) = output.as_chunks_mut::<48>();
    for (source, target) in blocks.iter().zip(targets) {
        // SAFETY: ld4 reads 64 bytes; st3 writes 48. These byte intrinsics do not
        // require alignment, and the chunks establish both allocation bounds.
        unsafe {
            let channels = vld4q_u8(source.as_ptr());
            vst3q_u8(
                target.as_mut_ptr().cast(),
                uint8x16x3_t(channels.0, channels.1, channels.2),
            );
        }
    }
    pack_scalar(tail, target_tail);
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgba;

    #[test]
    fn scalar_and_dispatched_rgb_match_for_tails_unaligned_buffers_and_native_sizes() {
        for pixels in (0..65).chain([
            72 * 72,
            80 * 80,
            96 * 96,
            120 * 120,
            144 * 112,
            800 * 100,
            1200 * 100,
            196 * 196,
        ]) {
            for offset in 0..16 {
                let storage: Vec<_> = (0..pixels * 4 + offset)
                    .map(|i| (i * 37 + i / 17) as u8)
                    .collect();
                let source = &storage[offset..];
                let expected: Vec<_> = source
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .flat_map(|p| p[..3].iter().copied())
                    .collect();
                let mut target = vec![MaybeUninit::new(0xa5); pixels * 3 + offset + 16];
                pack_rgb(source, &mut target[offset..offset + pixels * 3]);
                let actual: Vec<_> = target.iter().map(|v| unsafe { v.assume_init() }).collect();
                assert_eq!(&actual[offset..offset + pixels * 3], expected);
                assert!(
                    actual[..offset]
                        .iter()
                        .chain(&actual[offset + pixels * 3..])
                        .all(|v| *v == 0xa5)
                );
                let mut scalar = vec![MaybeUninit::uninit(); pixels * 3];
                pack_scalar(source, &mut scalar);
                assert_eq!(
                    scalar
                        .into_iter()
                        .map(|v| unsafe { v.assume_init() })
                        .collect::<Vec<_>>(),
                    expected
                );
            }
        }
        let image = RgbaImage::from_raw(1, 1, vec![1, 2, 3, 127, 99, 98, 97, 96]).unwrap();
        assert_eq!(&*rgb_pixels(&image), [1, 2, 3]);
    }

    #[test]
    fn opacity_metadata_is_bounded_and_does_not_retain_pixel_buffers() {
        let mut cache = OpacityCache::default();
        let image = Arc::new(RgbaImage::from_pixel(8, 8, Rgba([1, 2, 3, 255])));
        assert!(cache.opaque(&image));
        assert!(cache.opaque(&image));
        assert_eq!(Arc::strong_count(&image), 1);
        let weak = Arc::downgrade(&image);
        drop(image);
        assert!(weak.upgrade().is_none());
        let image = Arc::new(RgbaImage::from_pixel(8, 8, Rgba([1, 2, 3, 127])));
        assert!(!cache.opaque(&image));
        for _ in 0..100 {
            cache.opaque(&Arc::new(RgbaImage::from_pixel(1, 1, Rgba([0, 0, 0, 255]))));
        }
        assert_eq!(cache.0.len(), 64);
        cache.clear();
        assert!(cache.0.is_empty());
    }
}
