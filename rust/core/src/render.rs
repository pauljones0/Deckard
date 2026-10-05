use crate::{
    animation::{GifPlayer, VideoPlayer},
    cache::ByteCache,
};
use anyhow::{Context, Result};
use elgato_streamdeck::info::{ImageFormat, ImageMirroring, ImageMode, ImageRotation, Kind};
use image::{
    DynamicImage, Rgba, RgbaImage,
    imageops::{self, FilterType},
};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    hash::{Hash, Hasher},
    path::PathBuf,
    sync::Arc,
    time::Instant,
};
#[derive(Clone, PartialEq)]
pub struct RenderConfig {
    pub page: Value,
    pub sticky: Value,
    pub states: HashMap<String, usize>,
    pub pressed: HashSet<String>,
    pub rotation: u16,
    pub sleeping: bool,
    pub revision: u64,
}
#[derive(Clone)]
pub struct Tile {
    pub key: u8,
    pub rgb: Arc<[u8]>,
    pub encoded: Arc<[u8]>,
    pub width: u32,
    pub height: u32,
    pub identity: u64,
}
#[derive(Clone)]
pub struct Frame {
    pub tiles: Vec<Tile>,
    pub strip: Option<Tile>,
    pub revision: u64,
}
enum Asset {
    Static(Arc<RgbaImage>),
    Gif(Box<GifPlayer>),
    Video(VideoPlayer),
    Failed,
}
impl Asset {
    fn memory(&self) -> usize {
        match self {
            Self::Static(p) => p.as_raw().len(),
            Self::Gif(p) => p.memory(),
            Self::Video(p) => p.memory(),
            Self::Failed => 0,
        }
    }
}
type GlyphCache = HashMap<(String, char, u32), (fontdue::Metrics, Arc<[u8]>)>;
pub struct Renderer {
    font: fontdue::Font,
    assets: HashMap<PathBuf, Asset>,
    encoded: ByteCache<u64>,
    used: HashSet<PathBuf>,
    pub animated: bool,
    asset_frames: HashMap<PathBuf, Arc<RgbaImage>>,
    last_revision: u64,
    gif_frames: HashMap<PathBuf, (u64, Arc<RgbaImage>)>,
    previous_config: Option<(Kind, RenderConfig)>,
    previous_frame: Option<Frame>,
    scaled: HashMap<(usize, u32, u32, String, String), Arc<RgbaImage>>,
    scaled_bytes: usize,
    stamps: HashMap<PathBuf, (u64, std::time::SystemTime)>,
    glyphs: std::cell::RefCell<GlyphCache>,
    pub errors: Vec<String>,
    fonts: std::cell::RefCell<HashMap<String, Arc<fontdue::Font>>>,
}
pub fn layout(kind: Kind, rotation: u16) -> (u8, u8) {
    let (rows, cols) = kind.key_layout();
    if rotation % 180 == 90 {
        (cols, rows)
    } else {
        (rows, cols)
    }
}
pub fn logical_index(kind: Kind, physical: u8, rotation: u16) -> u8 {
    let (rows, cols) = kind.key_layout();
    let x = physical % cols;
    let y = physical / cols;
    match rotation % 360 {
        90 => x * rows + (rows - 1 - y),
        180 => (rows - 1 - y) * cols + (cols - 1 - x),
        270 => (cols - 1 - x) * rows + y,
        _ => physical,
    }
}
pub fn logical_input(kind: Kind, physical: u8, rotation: u16) -> String {
    let (_, cols) = layout(kind, rotation);
    let index = logical_index(kind, physical, rotation);
    format!("{}x{}", index % cols, index / cols)
}
pub fn effective_input<'a>(config: &'a RenderConfig, family: &str, input: &str) -> &'a Value {
    let sticky = &config.sticky[family][input];
    if sticky["states"].as_object().is_some_and(|s| {
        s.values().any(|v| {
            v["actions"].as_array().is_some_and(|a| !a.is_empty())
                || v["media"]["path"].as_str().is_some()
                || v["background"]["color"].is_array()
                || v["labels"].as_object().is_some_and(|o| {
                    o.values().any(|label| {
                        label
                            .as_object()
                            .is_some_and(|l| l.values().any(|v| !v.is_null()))
                    })
                })
        })
    }) {
        sticky
    } else {
        &config.page[family][input]
    }
}
pub fn active_state(config: &RenderConfig, family: &str, input: &str) -> usize {
    let key = format!("{family}/{input}");
    let data = effective_input(config, family, input);
    let wanted = config
        .states
        .get(&key)
        .copied()
        .or_else(|| data["active-state"].as_u64().map(|v| v as usize))
        .unwrap_or(0);
    if data["states"].get(wanted.to_string()).is_some() {
        wanted
    } else {
        0
    }
}
impl Renderer {
    pub fn new(budget: usize) -> Result<Self> {
        Ok(Self {
            font: fontdue::Font::from_bytes(
                include_bytes!("../../../Assets/Fonts/Roboto-Regular.ttf") as &[u8],
                fontdue::FontSettings::default(),
            )
            .map_err(anyhow::Error::msg)?,
            assets: HashMap::new(),
            encoded: ByteCache::new(budget),
            used: HashSet::new(),
            animated: false,
            asset_frames: HashMap::new(),
            last_revision: u64::MAX,
            gif_frames: HashMap::new(),
            previous_config: None,
            previous_frame: None,
            scaled: HashMap::new(),
            scaled_bytes: 0,
            stamps: HashMap::new(),
            glyphs: std::cell::RefCell::new(HashMap::new()),
            errors: Vec::new(),
            fonts: std::cell::RefCell::new(HashMap::new()),
        })
    }
    pub fn release_media(&mut self) {
        self.assets.clear();
        self.asset_frames.clear();
        self.stamps.clear();
        self.gif_frames.clear();
        self.previous_frame = None;
        self.previous_config = None;
        self.scaled.clear();
        self.scaled_bytes = 0;
        self.animated = false;
    }
    fn asset_playback(
        &mut self,
        path: &str,
        width: u32,
        height: u32,
        options: &Value,
    ) -> Option<Arc<RgbaImage>> {
        if path.is_empty() {
            return None;
        }
        let path = PathBuf::from(path);
        self.used.insert(path.clone());
        if let Some(frame) = self.asset_frames.get(&path) {
            return Some(frame.clone());
        }
        if !self.assets.contains_key(&path) {
            // Static and decoded GIF canvases share a hard per-asset bound.
            let loaded = (|| -> Result<Asset> {
                let extension = path
                    .extension()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_lowercase();
                if extension == "gif" {
                    let mut player = GifPlayer::new(&path)?;
                    player.looping = options["loop"].as_bool().unwrap_or(true);
                    player.fps = options["fps"].as_u64().unwrap_or(30).clamp(1, 60) as u32;
                    return Ok(Asset::Gif(Box::new(player)));
                }
                if ["mp4", "mkv", "webm", "mov", "avi"].contains(&extension.as_str()) {
                    return Ok(Asset::Video(VideoPlayer::new(
                        &path,
                        width,
                        height,
                        options["fps"].as_u64().unwrap_or(30).clamp(1, 60) as u32,
                        options["loop"].as_bool().unwrap_or(true),
                    )?));
                }
                if extension == "svg" {
                    let source = std::fs::read(&path)?;
                    anyhow::ensure!(source.len() <= 4 * 1024 * 1024, "SVG source exceeds limit");
                    let mut options = resvg::usvg::Options::default();
                    std::sync::Arc::make_mut(&mut options.fontdb).load_font_data(
                        include_bytes!("../../../Assets/Fonts/Roboto-Regular.ttf").to_vec(),
                    );
                    let tree = resvg::usvg::Tree::from_data(&source, &options)?;
                    let factor = (width as f32 / tree.size().width())
                        .max(height as f32 / tree.size().height());
                    let sw = (tree.size().width() * factor).ceil().clamp(1.0, 4096.0) as u32;
                    let sh = (tree.size().height() * factor).ceil().clamp(1.0, 4096.0) as u32;
                    anyhow::ensure!(
                        u64::from(sw) * u64::from(sh) <= 4 * 1024 * 1024,
                        "SVG canvas exceeds limit"
                    );
                    let mut pixmap = resvg::tiny_skia::Pixmap::new(sw, sh).context("SVG canvas")?;
                    resvg::render(
                        &tree,
                        resvg::tiny_skia::Transform::from_scale(factor, factor),
                        &mut pixmap.as_mut(),
                    );
                    let mut pixels = pixmap.take();
                    for pixel in pixels.as_chunks_mut::<4>().0 {
                        let alpha = u32::from(pixel[3]);
                        for c in pixel.iter_mut().take(3) {
                            *c = (u32::from(*c) * 255)
                                .checked_div(alpha)
                                .unwrap_or(0)
                                .min(255) as u8;
                        }
                    }
                    return Ok(Asset::Static(Arc::new(
                        RgbaImage::from_raw(sw, sh, pixels).context("SVG image")?,
                    )));
                }
                let mut reader = image::ImageReader::open(&path)?.with_guessed_format()?;
                let mut limits = image::Limits::default();
                limits.max_image_width = Some(4096);
                limits.max_image_height = Some(4096);
                limits.max_alloc = Some(32 * 1024 * 1024);
                reader.limits(limits);
                Ok(Asset::Static(Arc::new(reader.decode()?.to_rgba8())))
            })();
            let loaded = loaded.unwrap_or_else(|error| {
                self.errors
                    .push(format!("Media {}: {error}", path.display()));
                Asset::Failed
            });
            let total = self.assets.values().map(Asset::memory).sum::<usize>();
            let loaded = if total.saturating_add(loaded.memory()) <= 64 * 1024 * 1024 {
                loaded
            } else {
                self.errors.push(format!(
                    "Media {} exceeds the decoded cache budget",
                    path.display()
                ));
                Asset::Failed
            };
            if let Ok(meta) = std::fs::metadata(&path)
                && let Ok(modified) = meta.modified()
            {
                self.stamps.insert(path.clone(), (meta.len(), modified));
            }
            self.assets.insert(path.clone(), loaded);
        }
        let asset = self.assets.get_mut(&path)?;
        let result = match asset {
            Asset::Static(image) => Some(image.clone()),
            Asset::Gif(player) => {
                self.animated |= player.running();
                match player.tick(Instant::now()).map(|_| ()) {
                    Ok(()) => {
                        let sequence = player.sequence;
                        let frame = player.current_image();
                        let cached = self
                            .gif_frames
                            .entry(path.clone())
                            .or_insert_with(|| (sequence, Arc::new(frame.clone())));
                        if cached.0 != sequence {
                            *cached = (sequence, Arc::new(frame.clone()));
                        }
                        Some(cached.1.clone())
                    }
                    Err(error) => {
                        self.errors.push(format!("GIF {}: {error}", path.display()));
                        *asset = Asset::Failed;
                        None
                    }
                }
            }
            Asset::Video(player) => {
                let running = player.running();
                self.animated |= running;
                let frame = player.current().map(|(_, p)| p);
                if !running && frame.is_none() {
                    self.errors
                        .push(format!("Video {} produced no frames", path.display()));
                    *asset = Asset::Failed;
                }
                frame
            }
            Asset::Failed => None,
        };
        if let Some(frame) = &result {
            self.asset_frames.insert(path, frame.clone());
        }
        result
    }
    pub fn render(&mut self, kind: Kind, config: &RenderConfig) -> Result<Frame> {
        if config.revision != self.last_revision {
            self.last_revision = config.revision;
            self.assets.retain(|path, asset| {
                !matches!(asset, Asset::Failed)
                    && std::fs::metadata(path)
                        .ok()
                        .and_then(|m| m.modified().ok().map(|t| (m.len(), t)))
                        .is_some_and(|stamp| self.stamps.get(path) == Some(&stamp))
            });
        }
        self.gif_frames
            .retain(|path, _| self.assets.contains_key(path));
        self.used.clear();
        let previous_assets = std::mem::take(&mut self.asset_frames);
        self.animated = false;
        let slideshow = config.page["background"]["media-paths"]
            .as_array()
            .is_some_and(|paths| paths.len() > 1);
        if !slideshow
            && self
                .previous_config
                .as_ref()
                .is_some_and(|(previous_kind, previous)| {
                    *previous_kind == kind && previous == config
                })
        {
            // Advance playback clocks, but reuse the composed frame until its pixels change.
            let paths: Vec<_> = self.assets.keys().cloned().collect();
            for path in paths {
                self.asset_playback(&path.to_string_lossy(), 1, 1, &Value::Null);
            }
            if self.asset_frames.len() == previous_assets.len()
                && self.asset_frames.iter().all(|(path, frame)| {
                    previous_assets
                        .get(path)
                        .is_some_and(|previous| Arc::ptr_eq(frame, previous))
                })
                && let Some(frame) = &self.previous_frame
            {
                return Ok(frame.clone());
            }
        }
        self.scaled.clear();
        self.scaled_bytes = 0;
        let (rows, cols) = layout(kind, config.rotation);
        let mut fmt = kind.key_image_format();
        if kind == Kind::PlusXl {
            fmt.size = (112, 112);
        }
        let (fw, fh) = if config.rotation % 180 == 90 {
            (fmt.size.1, fmt.size.0)
        } else {
            fmt.size
        };
        let w = fw.max(72) as u32;
        let h = fh.max(72) as u32;
        let background = &config.page["background"];
        let paths = background["media-paths"].as_array();
        let interval = background["slideshow-interval"].as_i64().unwrap_or(10);
        let seconds = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let slideshow = paths.and_then(|paths| {
            if paths.len() < 2 {
                return None;
            }
            self.animated = true;
            let index = if interval <= 0 {
                0
            } else {
                let step = seconds / interval as u64;
                if background["slideshow-order"].as_str() == Some("shuffle") {
                    let cycle = step / paths.len() as u64;
                    let mut order: Vec<_> = (0..paths.len()).collect();
                    let mut seed = cycle.wrapping_add(0x9e3779b97f4a7c15);
                    for i in (1..order.len()).rev() {
                        seed ^= seed << 13;
                        seed ^= seed >> 7;
                        seed ^= seed << 17;
                        order.swap(i, seed as usize % (i + 1));
                    }
                    order[step as usize % paths.len()]
                } else {
                    step as usize % paths.len()
                }
            };
            paths.get(index)
        });
        let bgpath = if config.sleeping
            || background["enable"].as_bool() == Some(false)
            || background["show"].as_bool() == Some(false)
        {
            ""
        } else {
            slideshow
                .and_then(|v| v.as_str().or_else(|| v["path"].as_str()))
                .or_else(|| background["media-path"].as_str())
                .unwrap_or("")
        };
        let bgview = slideshow
            .map(|v| &v["view"])
            .filter(|v| v.is_object())
            .unwrap_or(&background["view"]);
        let (sx, sy) = if kind == Kind::Plus {
            (116, 34)
        } else if kind == Kind::Neo {
            (32, 36)
        } else {
            (36, 36)
        };
        let (sx, sy) = if config.rotation % 180 == 90 {
            (sy, sx)
        } else {
            (sx, sy)
        };
        let grid = (
            w * u32::from(cols) + sx * u32::from(cols - 1),
            h * u32::from(rows) + sy * u32::from(rows - 1),
        );
        let extend = background["extend-to-touchscreen"]
            .as_bool()
            .unwrap_or(false);
        let strip_size = kind.lcd_strip_size().map(|(sw, sh)| {
            if kind == Kind::PlusXl {
                (sh as u32, sw as u32)
            } else {
                (sw as u32, sh as u32)
            }
        });
        let band = crate::geometry::background_band(
            grid,
            if extend { strip_size } else { None },
            config.rotation,
            kind == Kind::Plus,
            if kind == Kind::Plus { 34 } else { 36 },
        );
        let whole = self
            .asset_playback(bgpath, band.canvas.0, band.canvas.1, background)
            .map(|img| fit(&img, band.canvas.0, band.canvas.1, bgview));
        let mut tiles = Vec::new();
        for physical in 0..kind.key_count() {
            let logical = logical_index(kind, physical, config.rotation);
            let input = logical_input(kind, physical, config.rotation);
            let data = effective_input(config, "keys", &input);
            let state = &data["states"][active_state(config, "keys", &input).to_string()];
            let mut tile = if let Some(bg) = &whole {
                imageops::crop_imm(
                    bg,
                    band.key_origin.0 + u32::from(logical % cols) * (w + sx),
                    band.key_origin.1 + u32::from(logical / cols) * (h + sy),
                    w,
                    h,
                )
                .to_image()
            } else {
                RgbaImage::from_pixel(w, h, Rgba([0, 0, 0, 255]))
            };
            if config.sleeping {
                tile.fill(0)
            } else {
                let color = color(&state["background"]["color"], [0, 0, 0, 0]);
                overlay_color(&mut tile, color);
                for path in [
                    state["background"]["image"].as_str(),
                    state["media"]["path"].as_str(),
                ]
                .into_iter()
                .flatten()
                {
                    if let Some(img) = self.asset_playback(path, w, h, &state["media"]) {
                        let img = self.scaled_media(&img, w, h, &state["media"], "cover");
                        imageops::overlay(&mut tile, img.as_ref(), 0, 0);
                    }
                }
                self.labels(&mut tile, state);
                if config.pressed.contains(&input)
                    && config.page["native-options"]["shrink-on-press"]
                        .as_bool()
                        .unwrap_or(true)
                    && !state["media"]["no-shrink"].as_bool().unwrap_or(false)
                {
                    let small = imageops::resize(
                        &tile,
                        (w * 9 / 10).max(1),
                        (h * 9 / 10).max(1),
                        FilterType::Triangle,
                    );
                    tile = RgbaImage::from_pixel(w, h, Rgba([0, 0, 0, 255]));
                    imageops::overlay(
                        &mut tile,
                        &small,
                        i64::from((w - small.width()) / 2),
                        i64::from((h - small.height()) / 2),
                    );
                }
            }
            tiles.push(self.encode(physical, tile, fmt, config.rotation)?);
        }
        let strip = if let Some((sw, sh)) = kind.lcd_strip_size() {
            let (sw, sh) = if kind == Kind::PlusXl {
                (sh, sw)
            } else {
                (sw, sh)
            };
            let (sw, sh) = if config.rotation % 180 == 90 {
                (sh, sw)
            } else {
                (sw, sh)
            };
            let sw = sw as u32;
            let sh = sh as u32;
            let mut strip = RgbaImage::from_pixel(sw, sh, Rgba([0, 0, 0, 255]));
            if !config.sleeping {
                if extend && let Some(whole) = &whole {
                    let (l, t, r, b) = band.crop;
                    let crop = imageops::crop_imm(whole, l, t, r - l, b - t).to_image();
                    let crop = imageops::resize(&crop, sw, sh, FilterType::Triangle);
                    imageops::overlay(&mut strip, &crop, 0, 0);
                }
                if kind == Kind::Neo {
                    let data = effective_input(config, "infobar", "0");
                    let state = &data["states"][active_state(config, "infobar", "0").to_string()];
                    overlay_color(
                        &mut strip,
                        color(&state["background"]["color"], [0, 0, 0, 0]),
                    );
                    if let Some(path) = state["media"]["path"].as_str()
                        && let Some(image) = self.asset_playback(path, sw, sh, &state["media"])
                    {
                        imageops::overlay(
                            &mut strip,
                            self.scaled_media(&image, sw, sh, &state["media"], "contain")
                                .as_ref(),
                            0,
                            0,
                        );
                    }
                    self.labels(&mut strip, state);
                    if state["labels"].is_null() {
                        self.text(
                            &mut strip,
                            config.page["name"].as_str().unwrap_or("Deckard"),
                            16.0,
                            [255, 255, 255, 255],
                            (0.5, 0.5),
                        );
                    }
                } else {
                    let count = kind.encoder_count();
                    for i in 0..count {
                        let data = effective_input(config, "dials", &i.to_string());
                        let state = &data["states"]
                            [active_state(config, "dials", &i.to_string()).to_string()];
                        let vertical = sh > sw;
                        let (tw, th) = if vertical {
                            (sw, sh / u32::from(count))
                        } else {
                            (sw / u32::from(count), sh)
                        };
                        let mut part = RgbaImage::from_pixel(
                            tw,
                            th,
                            Rgba(color(&state["background"]["color"], [0, 0, 0, 0])),
                        );
                        if let Some(path) = state["media"]["path"].as_str()
                            && let Some(img) = self.asset_playback(path, tw, th, &state["media"])
                        {
                            imageops::overlay(
                                &mut part,
                                self.scaled_media(&img, tw, th, &state["media"], "contain")
                                    .as_ref(),
                                0,
                                0,
                            )
                        }
                        self.labels(&mut part, state);
                        let slot = if config.rotation % 360 == 270 {
                            count - 1 - i
                        } else {
                            i
                        };
                        imageops::overlay(
                            &mut strip,
                            &part,
                            if vertical {
                                0
                            } else {
                                i64::from(tw * u32::from(slot))
                            },
                            if vertical {
                                i64::from(th * u32::from(slot))
                            } else {
                                0
                            },
                        );
                    }
                }
            }
            Some(self.encode(
                255,
                strip,
                kind.lcd_image_format().context("LCD format missing")?,
                config.rotation,
            )?)
        } else {
            None
        };
        self.assets.retain(|p, _| self.used.contains(p));
        self.stamps.retain(|p, _| self.used.contains(p));
        self.gif_frames.retain(|path, _| self.used.contains(path));
        let frame = Frame {
            tiles,
            strip,
            revision: config.revision,
        };
        self.previous_config = Some((kind, config.clone()));
        self.previous_frame = Some(frame.clone());
        Ok(frame)
    }
    fn scaled_media(
        &mut self,
        source: &Arc<RgbaImage>,
        width: u32,
        height: u32,
        options: &Value,
        mode: &str,
    ) -> Arc<RgbaImage> {
        let key = (
            Arc::as_ptr(source) as usize,
            width,
            height,
            options.to_string(),
            mode.to_owned(),
        );
        if let Some(image) = self.scaled.get(&key) {
            return image.clone();
        }
        let image = Arc::new(media_layout(source, width, height, options, mode));
        if self.scaled_bytes.saturating_add(image.as_raw().len()) <= 16 * 1024 * 1024 {
            self.scaled_bytes += image.as_raw().len();
            self.scaled.insert(key, image.clone());
        }
        image
    }
    fn encode(
        &mut self,
        key: u8,
        img: RgbaImage,
        format: ImageFormat,
        user_rotation: u16,
    ) -> Result<Tile> {
        let preview = DynamicImage::ImageRgba8(img.clone()).to_rgb8();
        let rgb = DynamicImage::ImageRgba8(rotate_ccw(img, user_rotation)).to_rgb8();
        let (width, height) = rgb.dimensions();
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        rgb.as_raw().hash(&mut hasher);
        format.hash(&mut hasher);
        let identity = hasher.finish();
        let encoded = if let Some(bytes) = self.encoded.get(&identity) {
            bytes
        } else {
            let bytes = match format.mode {
                ImageMode::JPEG => {
                    let rotation = match format.rotation {
                        ImageRotation::Rot0 => 0,
                        ImageRotation::Rot90 => 270,
                        ImageRotation::Rot180 => 180,
                        ImageRotation::Rot270 => 90,
                    };
                    let flips = match format.mirror {
                        ImageMirroring::None => (false, false),
                        ImageMirroring::X => (true, false),
                        ImageMirroring::Y => (false, true),
                        ImageMirroring::Both => (true, true),
                    };
                    crate::media::encode_rgb(rgb.as_raw(), width, height, rotation, flips, 90)
                        .map_err(anyhow::Error::msg)?
                }
                ImageMode::PNG => {
                    use image::ImageEncoder;
                    let mut out = Vec::new();
                    image::codecs::png::PngEncoder::new(&mut out).write_image(
                        rgb.as_raw(),
                        width,
                        height,
                        image::ColorType::Rgb8.into(),
                    )?;
                    out
                }
                ImageMode::BMP => elgato_streamdeck::images::convert_image_with_format(
                    format,
                    DynamicImage::ImageRgb8(rgb.clone()),
                )?,
                ImageMode::None => vec![],
            };
            let bytes: Arc<[u8]> = bytes.into();
            self.encoded.put(identity, bytes.clone());
            bytes
        };
        Ok(Tile {
            key,
            width: preview.width(),
            height: preview.height(),
            rgb: preview.into_raw().into(),
            encoded,
            identity,
        })
    }
    fn labels(&self, img: &mut RgbaImage, state: &Value) {
        for (position, y) in [("top", 0.12), ("center", 0.5), ("bottom", 0.87)] {
            let label = &state["labels"][position];
            if let Some(text) = label["text"].as_str() {
                self.text_font(
                    img,
                    (text, label["font-family"].as_str().unwrap_or("Roboto")),
                    label["font-size"].as_f64().unwrap_or(14.0).clamp(6.0, 72.0) as f32,
                    color(&label["color"], [255, 255, 255, 255]),
                    (0.5, y),
                )
            }
        }
    }
    fn text(&self, img: &mut RgbaImage, text: &str, size: f32, color: [u8; 4], pos: (f32, f32)) {
        self.text_font(img, (text, "Roboto"), size, color, pos);
    }
    fn text_font(
        &self,
        img: &mut RgbaImage,
        content: (&str, &str),
        size: f32,
        color: [u8; 4],
        pos: (f32, f32),
    ) {
        let (text, family) = content;
        let custom = if family == "Roboto" {
            None
        } else {
            let mut fonts = self.fonts.borrow_mut();
            if !fonts.contains_key(family) && fonts.len() < 32 {
                static DATABASE: std::sync::OnceLock<resvg::usvg::fontdb::Database> =
                    std::sync::OnceLock::new();
                let db = DATABASE.get_or_init(|| {
                    let mut db = resvg::usvg::fontdb::Database::new();
                    db.load_system_fonts();
                    db
                });
                let query = resvg::usvg::fontdb::Query {
                    families: &[resvg::usvg::fontdb::Family::Name(family)],
                    ..Default::default()
                };
                if let Some(id) = db.query(&query)
                    && let Some(Ok(font)) = db.with_face_data(id, |bytes, index| {
                        fontdue::Font::from_bytes(
                            bytes,
                            fontdue::FontSettings {
                                collection_index: index,
                                ..Default::default()
                            },
                        )
                    })
                {
                    fonts.insert(family.into(), Arc::new(font));
                }
            }
            fonts.get(family).cloned()
        };
        let font = custom.as_deref().unwrap_or(&self.font);
        let glyphs: Vec<_> = text
            .chars()
            .take(128)
            .map(|c| {
                let mut glyphs = self.glyphs.borrow_mut();
                if glyphs.len() >= 4096 {
                    glyphs.clear();
                }
                glyphs
                    .entry((family.to_owned(), c, size.to_bits()))
                    .or_insert_with(|| {
                        let (metrics, pixels) = font.rasterize(c, size);
                        (metrics, Arc::from(pixels))
                    })
                    .clone()
            })
            .collect();
        let width: f32 = glyphs.iter().map(|(m, _)| m.advance_width).sum();
        let mut x = (img.width() as f32 * pos.0 - width / 2.0) as i32;
        let baseline = (img.height() as f32 * pos.1 + size / 3.0) as i32;
        for (metrics, pixels) in glyphs {
            for gy in 0..metrics.height {
                for gx in 0..metrics.width {
                    let px = x + metrics.xmin + gx as i32;
                    let py = baseline - metrics.height as i32 - metrics.ymin + gy as i32;
                    if px < 0 || py < 0 || px >= img.width() as i32 || py >= img.height() as i32 {
                        continue;
                    }
                    let alpha =
                        u32::from(pixels[gy * metrics.width + gx]) * u32::from(color[3]) / 255;
                    let pixel = img.get_pixel_mut(px as u32, py as u32);
                    for (c, target) in color.iter().take(3).enumerate() {
                        pixel[c] = ((u32::from(*target) * alpha
                            + u32::from(pixel[c]) * (255 - alpha))
                            / 255) as u8;
                    }
                    pixel[3] = 255;
                }
            }
            x += metrics.advance_width as i32;
        }
    }
}
fn overlay_color(image: &mut RgbaImage, color: [u8; 4]) {
    let alpha = u32::from(color[3]);
    if alpha == 0 {
        return;
    }
    for pixel in image.pixels_mut() {
        for c in 0..3 {
            pixel[c] =
                ((u32::from(color[c]) * alpha + u32::from(pixel[c]) * (255 - alpha)) / 255) as u8;
        }
        pixel[3] = 255;
    }
}
pub fn color(value: &Value, default: [u8; 4]) -> [u8; 4] {
    let Some(values) = value.as_array() else {
        return default;
    };
    if values.len() < 3 {
        return default;
    }
    let mut result = default;
    for i in 0..values.len().min(4) {
        result[i] = values[i].as_u64().unwrap_or(default[i].into()).min(255) as u8;
    }
    if values.len() == 3 {
        result[3] = 255
    }
    result
}
fn rotate_ccw(img: RgbaImage, rotation: u16) -> RgbaImage {
    match rotation % 360 {
        90 => imageops::rotate270(&img),
        180 => imageops::rotate180(&img),
        270 => imageops::rotate90(&img),
        _ => img,
    }
}
fn media_layout(image: &RgbaImage, w: u32, h: u32, media: &Value, default_mode: &str) -> RgbaImage {
    if media["view"].is_object() {
        return fit(image, w, h, &media["view"]);
    }
    let size = media["size"].as_f64().unwrap_or(1.0).clamp(0.0, 2.0);
    let mut canvas = RgbaImage::new(w, h);
    if size == 0.0 {
        return canvas;
    }
    let (bw, bh) = (
        (f64::from(w) * size).max(1.0),
        (f64::from(h) * size).max(1.0),
    );
    let mode = media["fill-mode"].as_str().unwrap_or(default_mode);
    let (rw, rh) = if mode == "stretch" {
        (bw as u32, bh as u32)
    } else {
        let factor = if mode == "contain" {
            (bw / f64::from(image.width())).min(bh / f64::from(image.height()))
        } else {
            (bw / f64::from(image.width())).max(bh / f64::from(image.height()))
        };
        (
            (f64::from(image.width()) * factor).round().max(1.0) as u32,
            (f64::from(image.height()) * factor).round().max(1.0) as u32,
        )
    };
    // Cap oversized cover intermediates by cropping the source to visible aspect first.
    let resized = if u64::from(rw) * u64::from(rh) > 4 * 1024 * 1024 {
        fit(image, bw as u32, bh as u32, &Value::Null)
    } else {
        imageops::resize(image, rw, rh, FilterType::Triangle)
    };
    let halign = media["halign"].as_f64().unwrap_or(0.0).clamp(-1.0, 1.0);
    let valign = media["valign"].as_f64().unwrap_or(0.0).clamp(-1.0, 1.0);
    let x = ((f64::from(w) - f64::from(resized.width())) * (halign + 1.0) / 2.0) as i64;
    let y = ((f64::from(h) - f64::from(resized.height())) * (valign + 1.0) / 2.0) as i64;
    imageops::overlay(&mut canvas, &resized, x, y);
    canvas
}
fn viewport_rect(source: (u32, u32), canvas: (u32, u32), view: &Value) -> (f64, f64, f64, f64) {
    let (sw, sh) = (f64::from(source.0), f64::from(source.1));
    let (cw, ch) = (f64::from(canvas.0), f64::from(canvas.1));
    let scale = view["scale"]
        .as_f64()
        .filter(|v| v.is_finite())
        .unwrap_or(1.0)
        .clamp(0.25, 8.0);
    let (rw, rh) = if sw * ch > sh * cw {
        (sh * cw / ch / scale, sh / scale)
    } else {
        (sw / scale, sw * ch / cw / scale)
    };
    let x = view["x"]
        .as_f64()
        .filter(|v| v.is_finite())
        .unwrap_or(0.5)
        .clamp(0.0, 1.0);
    let y = view["y"]
        .as_f64()
        .filter(|v| v.is_finite())
        .unwrap_or(0.5)
        .clamp(0.0, 1.0);
    let mut left = x * sw - rw / 2.0;
    let mut top = y * sh - rh / 2.0;
    if rw <= sw {
        left = left.clamp(0.0, sw - rw);
    }
    if rh <= sh {
        top = top.clamp(0.0, sh - rh);
    }
    (left, top, left + rw, top + rh)
}
fn fit(img: &RgbaImage, w: u32, h: u32, view: &Value) -> RgbaImage {
    let (l, t, r, b) = viewport_rect((img.width(), img.height()), (w, h), view);
    let cl = l.max(0.0);
    let ct = t.max(0.0);
    let cr = r.min(f64::from(img.width()));
    let cb = b.min(f64::from(img.height()));
    let left = cl.floor() as u32;
    let top = ct.floor() as u32;
    let width = (cr.ceil() as u32)
        .saturating_sub(left)
        .max(1)
        .min(img.width() - left);
    let height = (cb.ceil() as u32)
        .saturating_sub(top)
        .max(1)
        .min(img.height() - top);
    let dx = (f64::from(w) * (cl - l) / (r - l)).round() as u32;
    let dy = (f64::from(h) * (ct - t) / (b - t)).round() as u32;
    let dw = (f64::from(w) * (cr - cl) / (r - l)).round().max(1.0) as u32;
    let dh = (f64::from(h) * (cb - ct) / (b - t)).round().max(1.0) as u32;
    let crop = imageops::crop_imm(img, left, top, width, height);
    let resized = imageops::resize(&crop.to_image(), dw.min(w), dh.min(h), FilterType::Triangle);
    if dx == 0 && dy == 0 && resized.width() == w && resized.height() == h {
        return resized;
    }
    let mut canvas = RgbaImage::new(w, h);
    imageops::overlay(&mut canvas, &resized, i64::from(dx), i64::from(dy));
    canvas
}
pub fn default_config(page: Value) -> RenderConfig {
    RenderConfig {
        page,
        sticky: json!({}),
        states: HashMap::new(),
        pressed: HashSet::new(),
        rotation: 0,
        sleeping: false,
        revision: 0,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unchanged_animation_reuses_composition_but_new_frames_and_settings_invalidate_it() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("animation.gif");
        {
            let mut file = std::fs::File::create(&path).unwrap();
            let mut encoder = gif::Encoder::new(&mut file, 2, 2, &[255, 0, 0, 0, 255, 0]).unwrap();
            for index in [0, 1] {
                encoder
                    .write_frame(&gif::Frame {
                        width: 2,
                        height: 2,
                        delay: 10,
                        buffer: vec![index; 4].into(),
                        ..Default::default()
                    })
                    .unwrap();
            }
        }
        let state = json!({"states":{"0":{"media":{"path":path,"fps":10,"loop":true}}}});
        let mut page = json!({"keys":{}});
        for key in 0..8 {
            page["keys"][format!("{}x{}", key % 4, key / 4)] = state.clone();
        }
        let mut config = default_config(page);
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        let first = renderer.render(Kind::Plus, &config).unwrap();
        assert_eq!(renderer.scaled.len(), 1, "shared media resizes once");
        let repeated = renderer.render(Kind::Plus, &config).unwrap();
        assert!(Arc::ptr_eq(&first.tiles[0].rgb, &repeated.tiles[0].rgb));
        if let Asset::Gif(player) = renderer.assets.get_mut(&path).unwrap() {
            player
                .tick(Instant::now() + std::time::Duration::from_millis(150))
                .unwrap();
        } else {
            panic!("GIF must be loaded");
        }
        let changed = renderer.render(Kind::Plus, &config).unwrap();
        assert_ne!(first.tiles[0].identity, changed.tiles[0].identity);
        assert_eq!(&changed.tiles[0].rgb[..3], &[0, 255, 0]);
        config.sleeping = true;
        let sleeping = renderer.render(Kind::Plus, &config).unwrap();
        assert!(sleeping.tiles[0].rgb.iter().all(|byte| *byte == 0));
        renderer.release_media();
        assert!(renderer.previous_frame.is_none());
    }
    #[test]
    fn persisted_media_layout_preserves_size_alignment_and_contain() {
        let image = RgbaImage::from_pixel(20, 10, Rgba([255, 0, 0, 255]));
        let out = media_layout(
            &image,
            100,
            100,
            &json!({"size":0.5,"fill-mode":"contain","halign":-1,"valign":-1}),
            "cover",
        );
        assert_eq!(out.get_pixel(0, 0).0, [255, 0, 0, 255]);
        assert_eq!(out.get_pixel(49, 24).0, [255, 0, 0, 255]);
        assert_eq!(out.get_pixel(50, 25).0, [0, 0, 0, 0]);
    }
    #[test]
    fn viewport_preserves_fork_source_center_and_zoom_out_letterboxes() {
        assert_eq!(
            viewport_rect(
                (1000, 500),
                (200, 100),
                &json!({"x":0.25,"y":0.5,"scale":2})
            ),
            (0.0, 125.0, 500.0, 375.0)
        );
        assert_eq!(
            viewport_rect((1000, 500), (200, 100), &json!({"scale":1})),
            (0.0, 0.0, 1000.0, 500.0)
        );
        let source = RgbaImage::from_pixel(100, 50, Rgba([255, 0, 0, 255]));
        let image = fit(&source, 200, 100, &json!({"scale":0.5}));
        assert_eq!(image.get_pixel(0, 0).0, [0, 0, 0, 0]);
        assert_eq!(image.get_pixel(100, 50).0, [255, 0, 0, 255]);
    }
    #[test]
    fn mapping_matches_the_forks_independent_direction_oracle() {
        for (rotation, wanted) in [
            (0, [0, 1, 2, 3, 4, 5, 6, 7]),
            (90, [1, 3, 5, 7, 0, 2, 4, 6]),
            (180, [7, 6, 5, 4, 3, 2, 1, 0]),
            (270, [6, 4, 2, 0, 7, 5, 3, 1]),
        ] {
            let actual: Vec<_> = (0..8)
                .map(|p| logical_index(Kind::Plus, p, rotation))
                .collect();
            assert_eq!(actual, wanted);
        }
    }
    #[test]
    fn native_strip_dimensions_and_key_size_match_updated_upstream() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        for kind in [Kind::Plus, Kind::PlusXl, Kind::Neo] {
            for rotation in [0, 90, 180, 270] {
                let mut config = default_config(json!({}));
                config.rotation = rotation;
                let frame = renderer.render(kind, &config).unwrap();
                let strip = frame.strip.unwrap();
                let decoded = image::load_from_memory(&strip.encoded).unwrap();
                let expected = if kind == Kind::PlusXl {
                    (100, 1200)
                } else {
                    kind.lcd_strip_size().unwrap()
                };
                assert_eq!(
                    (decoded.width(), decoded.height()),
                    (expected.0 as u32, expected.1 as u32)
                );
                if kind == Kind::PlusXl {
                    assert_eq!(frame.tiles[0].width, 112);
                }
            }
        }
    }
    #[test]
    fn additional_upstream_models_encode_physical_key_sizes_in_every_orientation() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        for (kind, size) in [
            (Kind::Studio, (80, 120)),
            (Kind::Mirabox293s, (85, 85)),
            (Kind::UlanziD200, (196, 196)),
        ] {
            for rotation in [0, 90, 180, 270] {
                let mut config = default_config(json!({}));
                config.rotation = rotation;
                let frame = renderer.render(kind, &config).unwrap();
                assert_eq!(frame.tiles.len(), kind.key_count() as usize);
                let decoded = image::load_from_memory(&frame.tiles[0].encoded).unwrap();
                assert_eq!((decoded.width(), decoded.height()), size);
            }
        }
    }
    #[test]
    fn rotated_mapping_is_a_permutation_for_every_supported_orientation() {
        for kind in [
            Kind::Original,
            Kind::Mini,
            Kind::Xl,
            Kind::Plus,
            Kind::PlusXl,
            Kind::Neo,
            Kind::Pedal,
            Kind::Studio,
            Kind::Mirabox293s,
            Kind::UlanziD200,
        ] {
            for rotation in [0, 90, 180, 270] {
                let indices: HashSet<_> = (0..kind.key_count())
                    .map(|p| logical_index(kind, p, rotation))
                    .collect();
                assert_eq!(indices.len(), kind.key_count() as usize);
                assert!(indices.iter().all(|i| *i < kind.key_count()));
            }
        }
    }
    #[test]
    fn sticky_content_overrides_normal_page_and_invalid_state_falls_back() {
        let mut config = default_config(
            json!({"keys":{"0x0":{"states":{"0":{"labels":{"bottom":{"text":"normal"}}}}}}}),
        );
        config.sticky = json!({"keys":{"0x0":{"active-state":999,"states":{"0":{"labels":{"bottom":{"text":"sticky"}}}}}}});
        assert_eq!(active_state(&config, "keys", "0x0"), 0);
        assert_eq!(
            effective_input(&config, "keys", "0x0")["states"]["0"]["labels"]["bottom"]["text"],
            "sticky"
        );
    }
}
