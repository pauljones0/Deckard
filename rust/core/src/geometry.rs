#[derive(Clone, Copy)]
pub enum SlotOrder {
    X,
    YDown,
    YUp,
}

pub fn slot_box(
    index: i64,
    count: i64,
    size: (i64, i64),
    order: SlotOrder,
) -> Option<(i64, i64, i64, i64)> {
    if count <= 0 || index < 0 || index >= count || size.0 <= 0 || size.1 <= 0 {
        return None;
    }
    match order {
        SlotOrder::X => Some((
            index.checked_mul(size.0)? / count,
            0,
            (index + 1).checked_mul(size.0)? / count,
            size.1,
        )),
        SlotOrder::YDown | SlotOrder::YUp => {
            let index = if matches!(order, SlotOrder::YUp) {
                count - 1 - index
            } else {
                index
            };
            Some((
                0,
                index.checked_mul(size.1)? / count,
                size.0,
                (index + 1).checked_mul(size.1)? / count,
            ))
        }
    }
}

pub fn slot_at(point: (i64, i64), count: i64, size: (i64, i64), order: SlotOrder) -> i64 {
    if count <= 0 || point.0 < 0 || point.1 < 0 || point.0 >= size.0 || point.1 >= size.1 {
        return -1;
    }
    let (position, extent) = if matches!(order, SlotOrder::X) {
        (point.0, size.0)
    } else {
        (point.1, size.1)
    };
    let slot = ((position as i128 * count as i128) / extent as i128) as i64;
    if matches!(order, SlotOrder::YUp) {
        count - 1 - slot
    } else {
        slot
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_rotations_cover_the_strip_and_reject_outside_touches() {
        assert_eq!(
            slot_box(0, 4, (100, 801), SlotOrder::YUp),
            Some((0, 600, 100, 801))
        );
        assert_eq!(slot_at((50, 800), 4, (100, 801), SlotOrder::YUp), 0);
        assert_eq!(slot_at((800, 50), 4, (800, 100), SlotOrder::X), -1);
        assert_eq!(slot_at((-1, 50), 4, (800, 100), SlotOrder::X), -1);
        assert_eq!(slot_box(0, 0, (800, 100), SlotOrder::X), None);
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Band {
    pub canvas: (u32, u32),
    pub key_origin: (u32, u32),
    pub crop: (u32, u32, u32, u32),
}
/// Preserve the fork's measured SD+ strip geometry and clockwise display mapping.
pub fn background_band(
    grid: (u32, u32),
    strip: Option<(u32, u32)>,
    rotation: u16,
    plus: bool,
    gap: u32,
) -> Band {
    let turned = rotation % 180 == 90;
    let (gw, gh) = if turned { (grid.1, grid.0) } else { grid };
    let Some((sw, sh)) = strip else {
        return Band {
            canvas: grid,
            key_origin: (0, 0),
            crop: (0, 0, 0, 0),
        };
    };
    let (span, offset, bh, gap) = if plus && gw == 828 {
        (867i64, -4i64, 114u32, 88u32)
    } else {
        (
            i64::from(gw),
            0,
            (f64::from(sh) * f64::from(gw) / f64::from(sw)).round() as u32,
            gap,
        )
    };
    let left = (i64::from(gw) - span).div_euclid(2) + offset;
    let overleft = (-left).max(0) as u32;
    let overright = (left + span - i64::from(gw)).max(0) as u32;
    let cw = gw + overleft + overright;
    let ch = gh + gap + bh;
    let l = (i64::from(overleft) + left) as u32;
    let t = gh + gap;
    let r = l + span as u32;
    let b = ch;
    match rotation % 360 {
        90 => Band {
            canvas: (ch, cw),
            key_origin: (ch - gh, overleft),
            crop: (ch - b, l, ch - t, r),
        },
        180 => Band {
            canvas: (cw, ch),
            key_origin: (cw - overleft - gw, ch - gh),
            crop: (cw - r, ch - b, cw - l, ch - t),
        },
        270 => Band {
            canvas: (ch, cw),
            key_origin: (0, cw - overleft - gw),
            crop: (t, cw - r, b, cw - l),
        },
        _ => Band {
            canvas: (cw, ch),
            key_origin: (overleft, 0),
            crop: (l, t, r, b),
        },
    }
}
#[cfg(test)]
mod band_tests {
    use super::*;
    #[test]
    fn plus_calibration_and_rotations_match_the_fork() {
        let band = background_band((828, 274), Some((800, 100)), 0, true, 34);
        assert_eq!(
            band,
            Band {
                canvas: (867, 476),
                key_origin: (24, 0),
                crop: (0, 362, 867, 476)
            }
        );
        let band = background_band((274, 828), Some((800, 100)), 90, true, 34);
        assert_eq!(band.canvas, (476, 867));
        assert_eq!(band.key_origin, (202, 24));
        assert_eq!(band.crop, (0, 0, 114, 867));
    }
}
