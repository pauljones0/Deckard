use crate::{
    animation::{GifPlayer, MAX_FPS, VideoPlayer},
    cache::ByteCache,
};
use anyhow::{Context, Result};
use elgato_streamdeck::info::{ImageFormat, ImageMirroring, ImageMode, ImageRotation, Kind};
use image::{
    DynamicImage, Pixel, Rgba, RgbaImage,
    imageops::{self, FilterType},
};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    hash::{Hash, Hasher},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
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
    pub key_size: Option<(usize, usize)>,
    pub max_fps: u32,
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
#[derive(Clone, Debug, Hash, Eq, PartialEq)]
struct AssetKey {
    path: PathBuf,
    width: u32,
    height: u32,
    fps: u32,
    looping: bool,
}
impl AssetKey {
    fn new(path: &str, width: u32, height: u32, options: &Value, cap: u32) -> Self {
        let extension = Path::new(path)
            .extension()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase();
        let sized = ["svg", "mp4", "mkv", "webm", "mov", "avi"].contains(&extension.as_str());
        let timed = extension == "gif" || (sized && extension != "svg");
        Self {
            path: path.into(),
            width: if sized { width } else { 0 },
            height: if sized { height } else { 0 },
            fps: if timed {
                options["fps"]
                    .as_u64()
                    .filter(|fps| *fps != 0)
                    .unwrap_or(u64::from(cap))
                    .clamp(1, u64::from(cap)) as u32
            } else {
                0
            },
            looping: !timed || options["loop"].as_bool().unwrap_or(true),
        }
    }
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
#[derive(Clone, Default)]
struct GlyphMetrics {
    width: usize,
    height: usize,
    xmin: i32,
    ymin: i32,
    advance_width: f32,
}
type FamilyGlyphs = HashMap<(char, u32), (GlyphMetrics, Arc<[u8]>)>;
#[derive(Default)]
struct GlyphCache {
    entries: HashMap<String, FamilyGlyphs>,
    bytes: usize,
    count: usize,
}
impl GlyphCache {
    fn get(
        &mut self,
        family: &str,
        c: char,
        size: f32,
        font: &ab_glyph::FontArc,
    ) -> (GlyphMetrics, Arc<[u8]>) {
        let key = (c, size.to_bits());
        if let Some(value) = self.entries.get(family).and_then(|glyphs| glyphs.get(&key)) {
            return value.clone();
        }
        let glyph = rasterize(font, c, size);
        let bytes = glyph.1.len() + std::mem::size_of::<GlyphMetrics>() + 64;
        if self.bytes + bytes > 2 * 1024 * 1024 || self.count >= 4096 {
            self.entries.clear();
            self.bytes = 0;
            self.count = 0;
        }
        self.bytes += bytes;
        self.count += 1;
        self.entries
            .entry(family.into())
            .or_default()
            .insert(key, glyph.clone());
        glyph
    }
}
fn rasterize(font: &ab_glyph::FontArc, c: char, pixels: f32) -> (GlyphMetrics, Arc<[u8]>) {
    use ab_glyph::{Font, ScaleFont};
    // ab_glyph measures scale as ascent minus descent; page font-size is em pixels.
    let scale =
        pixels * font.height_unscaled() / font.units_per_em().unwrap_or(font.height_unscaled());
    let scaled = font.as_scaled(scale);
    let id = scaled.glyph_id(c);
    let advance_width = scaled.h_advance(id);
    let Some(glyph) = font.outline_glyph(id.with_scale(scale)) else {
        return (
            GlyphMetrics {
                advance_width,
                ..Default::default()
            },
            Arc::from([]),
        );
    };
    let bounds = glyph.px_bounds();
    let width = bounds.width() as usize;
    let height = bounds.height() as usize;
    let mut bitmap = vec![0; width * height];
    glyph.draw(|x, y, alpha| {
        bitmap[y as usize * width + x as usize] = (alpha * 255.0).round() as u8
    });
    (
        GlyphMetrics {
            width,
            height,
            xmin: bounds.min.x as i32,
            ymin: -(bounds.min.y as i32) - height as i32,
            advance_width,
        },
        bitmap.into(),
    )
}
pub struct Renderer {
    font: ab_glyph::FontArc,
    assets: HashMap<AssetKey, Asset>,
    encoded: ByteCache<u64>,
    streaming_encoded: ByteCache<u64>,
    encoded_budget: usize,
    streaming_budget: usize,
    jpeg: crate::media::JpegEncoder,
    used: HashSet<AssetKey>,
    pub animated: bool,
    next_frame: Option<Instant>,
    max_fps: u32,
    asset_frames: HashMap<AssetKey, Arc<RgbaImage>>,
    last_revision: u64,
    gif_frames: HashMap<AssetKey, (u64, Arc<RgbaImage>)>,
    previous_config: Option<(Kind, Arc<RenderConfig>)>,
    previous_frame: Option<Frame>,
    scaled: HashMap<(usize, u32, u32, String, String), Arc<RgbaImage>>,
    scaled_bytes: usize,
    opacity: crate::pixels::OpacityCache,
    stamps: HashMap<AssetKey, (u64, std::time::SystemTime)>,
    glyphs: std::cell::RefCell<GlyphCache>,
    pub errors: Vec<String>,
    fonts: std::cell::RefCell<HashMap<String, Option<ab_glyph::FontArc>>>,
}
pub fn layout(kind: Kind, rotation: u16) -> (u8, u8) {
    let (rows, cols) = kind.key_layout();
    if rotation % 180 == 90 {
        (cols, rows)
    } else {
        (rows, cols)
    }
}
pub fn key_spacing(kind: Kind, rotation: u16) -> (u32, u32) {
    let gaps = match kind {
        Kind::Plus => (116, 34),
        Kind::Neo => (32, 36),
        _ => (36, 36),
    };
    if rotation % 180 == 90 {
        (gaps.1, gaps.0)
    } else {
        gaps
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
    effective_input_from(&config.page, &config.sticky, family, input)
}
pub fn effective_input_from<'a>(
    page: &'a Value,
    sticky: &'a Value,
    family: &str,
    input: &str,
) -> &'a Value {
    let sticky = &sticky[family][input];
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
        &page[family][input]
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
fn unchanged_sources(
    paths: &[&str],
    before: &HashMap<AssetKey, Arc<RgbaImage>>,
    after: &HashMap<AssetKey, Arc<RgbaImage>>,
) -> bool {
    paths.iter().filter(|p| !p.is_empty()).all(|path| {
        after
            .iter()
            .filter(|(key, _)| key.path == Path::new(path))
            .all(|(key, frame)| before.get(key).is_some_and(|old| Arc::ptr_eq(old, frame)))
            && before
                .keys()
                .filter(|key| key.path == Path::new(path))
                .all(|key| after.contains_key(key))
    })
}
impl Renderer {
    pub fn new(budget: usize) -> Result<Self> {
        Ok(Self {
            font: ab_glyph::FontArc::try_from_slice(include_bytes!(
                "../../../Assets/Fonts/Roboto-Regular.ttf"
            ))?,
            assets: HashMap::new(),
            encoded: ByteCache::new(budget),
            streaming_encoded: ByteCache::new((budget / 8).min(1024 * 1024)),
            encoded_budget: budget,
            streaming_budget: (budget / 8).min(1024 * 1024),
            jpeg: crate::media::JpegEncoder::new(90).map_err(anyhow::Error::msg)?,
            used: HashSet::new(),
            animated: false,
            next_frame: None,
            max_fps: MAX_FPS,
            asset_frames: HashMap::new(),
            last_revision: u64::MAX,
            gif_frames: HashMap::new(),
            previous_config: None,
            previous_frame: None,
            scaled: HashMap::new(),
            scaled_bytes: 0,
            opacity: crate::pixels::OpacityCache::default(),
            stamps: HashMap::new(),
            glyphs: std::cell::RefCell::new(GlyphCache::default()),
            errors: Vec::new(),
            fonts: std::cell::RefCell::new(HashMap::new()),
        })
    }
    pub fn release_media(&mut self) {
        self.opacity.clear();
        self.encoded.clear();
        self.streaming_encoded.clear();
        self.encoded.set_capacity(self.encoded_budget);
        self.assets.clear();
        self.asset_frames.clear();
        self.stamps.clear();
        self.gif_frames.clear();
        self.previous_frame = None;
        self.previous_config = None;
        self.scaled.clear();
        self.scaled_bytes = 0;
        self.animated = false;
        self.next_frame = None;
    }
    pub fn next_deadline(&self) -> Option<Instant> {
        self.next_frame
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
        self.asset_key(AssetKey::new(path, width, height, options, self.max_fps))
    }
    fn asset_key(&mut self, key: AssetKey) -> Option<Arc<RgbaImage>> {
        let path = &key.path;
        let (width, height) = (key.width, key.height);
        self.used.insert(key.clone());
        if let Some(frame) = self.asset_frames.get(&key) {
            return Some(frame.clone());
        }
        if !self.assets.contains_key(&key) {
            // Static and decoded GIF canvases share a hard per-asset bound.
            let loaded = (|| -> Result<Asset> {
                let extension = path
                    .extension()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_lowercase();
                if extension == "gif" {
                    let mut player = GifPlayer::new(path)?;
                    player.looping = key.looping;
                    player.fps = key.fps;
                    return Ok(Asset::Gif(Box::new(player)));
                }
                if ["mp4", "mkv", "webm", "mov", "avi"].contains(&extension.as_str()) {
                    return Ok(Asset::Video(VideoPlayer::new(
                        path,
                        width,
                        height,
                        key.fps,
                        key.looping,
                    )?));
                }
                if extension == "svg" {
                    let source = std::fs::read(path)?;
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
                let mut reader = image::ImageReader::open(path)?.with_guessed_format()?;
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
            if let Ok(meta) = std::fs::metadata(path)
                && let Ok(modified) = meta.modified()
            {
                self.stamps.insert(key.clone(), (meta.len(), modified));
            }
            self.assets.insert(key.clone(), loaded);
        }
        let asset = self.assets.get_mut(&key)?;
        let result = match asset {
            Asset::Static(image) => Some(image.clone()),
            Asset::Gif(player) => {
                self.animated |= player.running();
                match player.tick(Instant::now()).map(|_| ()) {
                    Ok(()) => {
                        if let Some(deadline) = player.next_deadline() {
                            self.next_frame =
                                Some(self.next_frame.map_or(deadline, |old| old.min(deadline)));
                        }
                        let sequence = player.sequence;
                        let frame = player.current_image();
                        let cached = self
                            .gif_frames
                            .entry(key.clone())
                            .or_insert_with(|| (sequence, Arc::new(frame.clone())));
                        if cached.0 != sequence && player.caught_up(Instant::now()) {
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
                if running {
                    let deadline = player.next_deadline();
                    self.next_frame =
                        Some(self.next_frame.map_or(deadline, |old| old.min(deadline)));
                }
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
            self.asset_frames.insert(key, frame.clone());
        }
        result
    }
    pub fn render(&mut self, kind: Kind, config: &RenderConfig) -> Result<Frame> {
        self.render_shared(kind, Arc::new(config.clone()))
    }
    pub fn render_shared(&mut self, kind: Kind, config: Arc<RenderConfig>) -> Result<Frame> {
        let config_ref = &config;
        if config.revision != self.last_revision {
            self.last_revision = config.revision;
            self.assets.retain(|path, asset| {
                !matches!(asset, Asset::Failed)
                    && std::fs::metadata(&path.path)
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
        self.next_frame = None;
        self.max_fps = config.max_fps.clamp(1, MAX_FPS);
        let slideshow = config.page["background"]["media-paths"]
            .as_array()
            .is_some_and(|paths| paths.len() > 1)
            && config.page["background"]["slideshow-interval"]
                .as_i64()
                .unwrap_or(10)
                > 0;
        let unchanged_config = !slideshow
            && self
                .previous_config
                .as_ref()
                .is_some_and(|(previous_kind, previous)| {
                    *previous_kind == kind
                        && (Arc::ptr_eq(previous, &config)
                            || previous.as_ref() == config_ref.as_ref())
                });
        if unchanged_config {
            // Advance playback clocks, but reuse the composed frame until its pixels change.
            let paths: Vec<_> = self.assets.keys().cloned().collect();
            for path in paths {
                self.asset_key(path);
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
        self.opacity.clear();
        let mut fmt = kind.key_image_format();
        if let Some(size) = config.key_size {
            fmt.size = size;
        }
        let (fw, fh) = if config.rotation % 180 == 90 {
            (fmt.size.1, fmt.size.0)
        } else {
            fmt.size
        };
        let w = fw.max(1) as u32;
        let h = fh.max(1) as u32;
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
            self.animated = interval > 0;
            if interval > 0 {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default();
                let until = Duration::from_secs(interval as u64)
                    - Duration::from_nanos(
                        (now.as_nanos() % Duration::from_secs(interval as u64).as_nanos()) as u64,
                    );
                self.next_frame = Some(Instant::now() + until);
            }
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
        let (sx, sy) = key_spacing(kind, config.rotation);
        let grid = (
            w * u32::from(cols) + sx * u32::from(cols - 1),
            h * u32::from(rows) + sy * u32::from(rows - 1),
        );
        let extend = background["extend-to-touchscreen"]
            .as_bool()
            .unwrap_or(false);
        let strip_size = kind.lcd_strip_size().map(|(sw, sh)| (sw as u32, sh as u32));
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
            let data = effective_input(config_ref, "keys", &input);
            let state = &data["states"][active_state(config_ref, "keys", &input).to_string()];
            let paths = [
                bgpath,
                state["background"]["image"].as_str().unwrap_or(""),
                state["media"]["path"].as_str().unwrap_or(""),
                state["native-background"]["artwork"].as_str().unwrap_or(""),
                state["native-visual"]["artwork"].as_str().unwrap_or(""),
            ];
            if unchanged_config
                && unchanged_sources(&paths, &previous_assets, &self.asset_frames)
                && let Some(tile) = self
                    .previous_frame
                    .as_ref()
                    .and_then(|f| f.tiles.iter().find(|t| t.key == physical))
            {
                tiles.push(tile.clone());
                continue;
            }
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
            let mut persistent = whole.is_none() || repeatable_media(bgpath, background);
            if config.sleeping {
                tile.fill(0)
            } else {
                self.native_visual(&mut tile, &state["native-background"]);
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
                        persistent &= repeatable_media(path, &state["media"]);
                        let img = self.scaled_media(&img, w, h, &state["media"], "cover");
                        self.overlay_image(&mut tile, &img, 0, 0);
                    }
                }
                self.native_visual(&mut tile, &state["native-visual"]);
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
                    overlay_rgba(
                        &mut tile,
                        &small,
                        i64::from((w - small.width()) / 2),
                        i64::from((h - small.height()) / 2),
                    );
                }
            }
            tiles.push(self.encode(physical, tile, fmt, config.rotation, persistent)?);
        }
        let mut strip_paths = vec![if extend { bgpath } else { "" }];
        for (family, input) in std::iter::once(("infobar", "0".to_owned()))
            .chain((0..kind.encoder_count()).map(|i| ("dials", i.to_string())))
        {
            let data = effective_input(config_ref, family, &input);
            let state = &data["states"][active_state(config_ref, family, &input).to_string()];
            for v in [&state["media"]["path"], &state["native-visual"]["artwork"]] {
                strip_paths.push(v.as_str().unwrap_or(""));
            }
        }
        let reusable_strip = unchanged_config
            && unchanged_sources(&strip_paths, &previous_assets, &self.asset_frames);
        let strip = if reusable_strip {
            self.previous_frame.as_ref().and_then(|f| f.strip.clone())
        } else if let Some((sw, sh)) = kind.lcd_strip_size() {
            let (sw, sh) = if config.rotation % 180 == 90 {
                (sh, sw)
            } else {
                (sw, sh)
            };
            let sw = sw as u32;
            let sh = sh as u32;
            let mut strip = RgbaImage::from_pixel(sw, sh, Rgba([0, 0, 0, 255]));
            let mut persistent = true;
            if !config.sleeping {
                if extend && let Some(whole) = &whole {
                    persistent &= repeatable_media(bgpath, background);
                    let (l, t, r, b) = band.crop;
                    let crop = imageops::crop_imm(whole, l, t, r - l, b - t).to_image();
                    let crop = imageops::resize(&crop, sw, sh, FilterType::Triangle);
                    overlay_rgba(&mut strip, &crop, 0, 0);
                }
                if kind == Kind::Neo {
                    let data = effective_input(config_ref, "infobar", "0");
                    let state =
                        &data["states"][active_state(config_ref, "infobar", "0").to_string()];
                    overlay_color(
                        &mut strip,
                        color(&state["background"]["color"], [0, 0, 0, 0]),
                    );
                    if let Some(path) = state["media"]["path"].as_str()
                        && let Some(image) = self.asset_playback(path, sw, sh, &state["media"])
                    {
                        persistent &= repeatable_media(path, &state["media"]);
                        let image = self.scaled_media(&image, sw, sh, &state["media"], "contain");
                        self.overlay_image(&mut strip, &image, 0, 0);
                    }
                    self.native_visual(&mut strip, &state["native-visual"]);
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
                        let data = effective_input(config_ref, "dials", &i.to_string());
                        let state = &data["states"]
                            [active_state(config_ref, "dials", &i.to_string()).to_string()];
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
                            persistent &= repeatable_media(path, &state["media"]);
                            let image = self.scaled_media(&img, tw, th, &state["media"], "contain");
                            self.overlay_image(&mut part, &image, 0, 0);
                        }
                        self.native_visual(&mut part, &state["native-visual"]);
                        self.labels(&mut part, state);
                        let slot = if config.rotation % 360 == 270 {
                            count - 1 - i
                        } else {
                            i
                        };
                        overlay_rgba(
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
                persistent,
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
        self.previous_config = Some((kind, config));
        self.previous_frame = Some(frame.clone());
        if !self.assets.iter().any(|(key, asset)| {
            !key.looping
                && match asset {
                    Asset::Gif(player) => player.running(),
                    Asset::Video(player) => player.running(),
                    _ => false,
                }
        }) {
            self.streaming_encoded.clear();
            self.encoded.set_capacity(self.encoded_budget);
        }
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
        if source.dimensions() == (width, height)
            && options["size"].as_f64().unwrap_or(1.0) == 1.0
            && !options["view"].is_object()
        {
            return source.clone();
        }
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
        persistent: bool,
    ) -> Result<Tile> {
        let (preview_width, preview_height) = img.dimensions();
        let preview = rgb_pixels(&img);
        let oriented = (!user_rotation.is_multiple_of(360)).then(|| {
            let image = image::ImageBuffer::<image::Rgb<u8>, _>::from_raw(
                preview_width,
                preview_height,
                preview.clone(),
            )
            .expect("RGB preview matches image dimensions");
            rotate_ccw(&image, user_rotation)
        });
        let (pixels, width, height) = oriented
            .as_ref()
            .map(|image| (image.as_raw().as_slice(), image.width(), image.height()))
            .unwrap_or((&preview, preview_width, preview_height));
        // This is a pixel identity, not a security signature or an untrusted map key.
        let mut format_hash = std::collections::hash_map::DefaultHasher::new();
        format.hash(&mut format_hash);
        (width, height).hash(&mut format_hash);
        let identity = xxhash_rust::xxh3::xxh3_64_with_seed(pixels, format_hash.finish());
        let previous = self
            .previous_frame
            .as_ref()
            .and_then(|frame| {
                if key == 255 {
                    frame.strip.as_ref()
                } else {
                    frame.tiles.get(key as usize)
                }
            })
            .filter(|tile| tile.key == key && tile.identity == identity);
        if !persistent {
            self.encoded
                .set_capacity(self.encoded_budget - self.streaming_budget);
        }
        let encoded = if let Some(bytes) = self.encoded.get(&identity) {
            bytes
        } else if let Some(bytes) = self.streaming_encoded.get(&identity) {
            if persistent {
                self.encoded.put(identity, bytes.clone());
            }
            bytes
        } else if let Some(tile) = previous {
            let bytes = tile.encoded.clone();
            if persistent {
                self.encoded.put(identity, bytes.clone());
            } else {
                self.streaming_encoded.put(identity, bytes.clone());
            }
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
                    self.jpeg
                        .encode(pixels, width, height, rotation, flips)
                        .map_err(anyhow::Error::msg)?
                }
                ImageMode::PNG => {
                    use image::ImageEncoder;
                    let mut out = Vec::new();
                    image::codecs::png::PngEncoder::new(&mut out).write_image(
                        pixels,
                        width,
                        height,
                        image::ColorType::Rgb8.into(),
                    )?;
                    out
                }
                ImageMode::BMP => elgato_streamdeck::images::convert_image_with_format(
                    format,
                    DynamicImage::ImageRgb8(
                        image::RgbImage::from_raw(width, height, pixels.to_vec())
                            .expect("RGB pixels match image dimensions"),
                    ),
                )?,
                ImageMode::None => vec![],
            };
            let bytes: Arc<[u8]> = bytes.into();
            if persistent {
                self.encoded.put(identity, bytes.clone());
            } else {
                // A small reuse window handles repeated pixels without keeping
                // an entire single-pass video's frame history in the main cache.
                self.streaming_encoded.put(identity, bytes.clone());
            }
            bytes
        };
        Ok(Tile {
            key,
            width: preview_width,
            height: preview_height,
            rgb: preview,
            encoded,
            identity,
        })
    }
    fn native_visual(&mut self, img: &mut RgbaImage, visual: &Value) {
        let (w, h) = img.dimensions();
        if !visual.is_object() {
            return;
        }
        if let Some(path) = visual["artwork"].as_str()
            && let Some(art) = self.asset_playback(path, w, h, &Value::Null)
        {
            let image = if let Some(crop) = visual["crop"].as_array().filter(|a| a.len() == 4) {
                let x = crop[0].as_u64().unwrap_or(0) as u32;
                let y = crop[1].as_u64().unwrap_or(0) as u32;
                let cols = crop[2].as_u64().unwrap_or(1).clamp(1, 64) as u32;
                let rows = crop[3].as_u64().unwrap_or(1).clamp(1, 64) as u32;
                let sx = visual["spacing"][0].as_u64().unwrap_or(0).min(512) as u32;
                let sy = visual["spacing"][1].as_u64().unwrap_or(0).min(512) as u32;
                let width = w * cols + sx * (cols - 1);
                let height = h * rows + sy * (rows - 1);
                if u64::from(width) * u64::from(height) > 4 * 1024 * 1024 {
                    self.errors
                        .push("Artwork grid exceeds the pixel budget".into());
                    return;
                }
                let full = self.scaled_media(
                    &art,
                    width,
                    height,
                    &serde_json::json!({"fill-mode":visual["fit"].as_str().unwrap_or("cover")}),
                    "cover",
                );
                Arc::new(
                    imageops::crop_imm(
                        full.as_ref(),
                        x.min(cols - 1) * (w + sx),
                        y.min(rows - 1) * (h + sy),
                        w,
                        h,
                    )
                    .to_image(),
                )
            } else {
                self.scaled_media(
                    &art,
                    w,
                    h,
                    &serde_json::json!({"fill-mode":"cover"}),
                    "cover",
                )
            };
            self.overlay_image(img, &image, 0, 0);
            if visual["darken"] == true {
                overlay_color(img, [0, 0, 0, 120]);
            }
        }
        if let Some(points) = visual["graph"].as_array() {
            let settings = &visual["settings"];
            let dynamic = settings["dynamic-scaling"].as_bool().unwrap_or(false);
            let ceiling = if dynamic {
                points
                    .iter()
                    .filter_map(Value::as_f64)
                    .fold(1.0_f64, f64::max)
            } else {
                100.0
            };
            let line = color(&settings["line-color"], [255, 255, 255, 255]);
            let fill = color(&settings["fill-color"], [255, 255, 255, 150]);
            let thickness = settings["line-width"].as_u64().unwrap_or(5).clamp(1, 32) as i32;
            let mut previous = None;
            for x in 0..w {
                let position = x as f64 / w.saturating_sub(1).max(1) as f64
                    * points.len().saturating_sub(1) as f64;
                let index = position.floor() as usize;
                let fract = position.fract();
                let a = points.get(index).and_then(Value::as_f64).unwrap_or(0.0);
                let b = points.get(index + 1).and_then(Value::as_f64).unwrap_or(a);
                let y = ((h - 1) as f64 * (1.0 - ((a + (b - a) * fract) / ceiling).clamp(0.0, 1.0)))
                    as i32;
                for fy in y.max(0) as u32..h {
                    blend_pixel(img, x, fy, fill);
                }
                if let Some((px, py)) = previous {
                    draw_line(img, (px, py), (x as i32, y), line, thickness);
                }
                previous = Some((x as i32, y));
            }
        }
        if let Some(progress) = visual["progress"].as_f64() {
            let margin = (w / 20).max(2);
            let y = h * 7 / 10;
            let height = (h / 15).max(3);
            let width = w.saturating_sub(2 * margin);
            for yy in y..(y + height).min(h) {
                for x in margin..(margin + width).min(w) {
                    img.put_pixel(
                        x,
                        yy,
                        Rgba(
                            if ((x - margin) as f64) < width as f64 * progress.clamp(0.0, 1.0) {
                                color(&visual["bar-color"], [90, 180, 245, 255])
                            } else {
                                [45, 45, 52, 255]
                            },
                        ),
                    );
                }
            }
        }
        if let Some(symbol) = visual["symbol"].as_str().filter(|s| !s.is_empty()) {
            let active = visual["active"].as_bool().unwrap_or(true);
            let rgba = if active {
                [240, 245, 255, 255]
            } else {
                [125, 135, 150, 255]
            };
            let cx = w as i32 / 2;
            let cy = h as i32 / 2;
            let size = w.min(h) as i32 / 5;
            match symbol {
                "Play" | "PlayPause" | "Next" | "Previous" => {
                    let direction = if symbol == "Previous" { -1 } else { 1 };
                    for dx in -size..=size {
                        let height = (size - dx).max(0) / 2;
                        for dy in -height..=height {
                            put_pixel(img, cx + dx * direction, cy + dy, rgba);
                        }
                    }
                    if ["Next", "Previous"].contains(&symbol) {
                        draw_line(
                            img,
                            (cx + direction * size, cy - size),
                            (cx + direction * size, cy + size),
                            rgba,
                            3,
                        );
                    }
                }
                "Pause" => {
                    for x in [-size / 2, size / 2] {
                        draw_line(
                            img,
                            (cx + x, cy - size),
                            (cx + x, cy + size),
                            rgba,
                            (size / 3).max(2),
                        );
                    }
                }
                "Stop" => {
                    for x in cx - size..cx + size {
                        for y in cy - size..cy + size {
                            put_pixel(img, x, y, rgba);
                        }
                    }
                }
                "record" | "obs" | "stream" => {
                    let rgba = if active { [235, 55, 75, 255] } else { rgba };
                    for x in -size..=size {
                        for y in -size..=size {
                            if x * x + y * y < size * size {
                                put_pixel(img, cx + x, cy + y, rgba);
                            }
                        }
                    }
                }
                "mute" => {
                    draw_line(
                        img,
                        (cx - size, cy - size),
                        (cx + size, cy + size),
                        [240, 65, 75, 255],
                        4,
                    );
                    draw_line(
                        img,
                        (cx - size, cy + size),
                        (cx + size, cy - size),
                        [240, 65, 75, 255],
                        4,
                    );
                }
                "brightness" | "brightness-adjust" => {
                    for angle in 0..8 {
                        let angle = angle as f64 * std::f64::consts::TAU / 8.0;
                        let (x, y) = (angle.cos(), angle.sin());
                        draw_line(
                            img,
                            (
                                cx + (x * size as f64 * 0.7) as i32,
                                cy + (y * size as f64 * 0.7) as i32,
                            ),
                            (
                                cx + (x * size as f64 * 1.3) as i32,
                                cy + (y * size as f64 * 1.3) as i32,
                            ),
                            rgba,
                            3,
                        );
                    }
                }
                _ => {
                    draw_line(img, (cx - size, cy - size), (cx + size, cy - size), rgba, 3);
                    draw_line(img, (cx + size, cy - size), (cx + size, cy + size), rgba, 3);
                    draw_line(img, (cx + size, cy + size), (cx - size, cy + size), rgba, 3);
                    draw_line(img, (cx - size, cy + size), (cx - size, cy - size), rgba, 3);
                    let text = match symbol {
                        "input" | "hotkey" => "Key",
                        "text" => "Aa",
                        "shell" => ">_",
                        "url" => "Web",
                        "page" => "Page",
                        "previous-page" => "Back",
                        "state" => "1/2",
                        "sleep" => "Zz",
                        "launch" => "App",
                        "delay" => "Wait",
                        "audio" => "Vol",
                        "mixer" => "Mix",
                        "scene" => "Scene",
                        "filter" => "FX",
                        "replay_buffer" => "Replay",
                        "virtual_camera" => "Cam",
                        "studio_mode" => "Studio",
                        other => other,
                    };
                    self.text(
                        img,
                        text,
                        (w.min(h) / 9).clamp(8, 16) as f32,
                        rgba,
                        (0.5, 0.5),
                    );
                }
            }
        }
    }
    fn overlay_image(&mut self, bottom: &mut RgbaImage, top: &Arc<RgbaImage>, x: i64, y: i64) {
        let opaque = self.opacity.opaque(top);
        overlay_rgba_inner(bottom, top, x, y, opaque);
    }
    fn labels(&self, img: &mut RgbaImage, state: &Value) {
        for (position, y) in [("top", 0.12), ("center", 0.5), ("bottom", 0.87)] {
            let label = &state["labels"][position];
            if let Some(text) = label["text"].as_str() {
                let outline = label["outline-width"].as_u64().unwrap_or(0).min(5) as i32;
                for dx in -outline..=outline {
                    for dy in -outline..=outline {
                        if dx * dx + dy * dy <= outline * outline && (dx != 0 || dy != 0) {
                            self.text_font(
                                img,
                                (text, label["font-family"].as_str().unwrap_or("Roboto")),
                                label["font-size"].as_f64().unwrap_or(14.0).clamp(6.0, 72.0) as f32,
                                color(&label["outline-color"], [0, 0, 0, 255]),
                                (
                                    0.5 + dx as f32 / img.width() as f32,
                                    y + dy as f32 / img.height() as f32,
                                ),
                            );
                        }
                    }
                }
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
            if !fonts.contains_key(family) && fonts.len() >= 32 {
                // Failed lookups must not occupy every slot and block a valid font.
                let missing = fonts
                    .iter()
                    .find_map(|(name, font)| font.is_none().then(|| name.clone()));
                if let Some(name) = missing {
                    fonts.remove(&name);
                }
            }
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
                let font = db
                    .query(&query)
                    .and_then(|id| {
                        db.with_face_data(id, |bytes, index| {
                            ab_glyph::FontVec::try_from_vec_and_index(bytes.to_vec(), index)
                                .map(ab_glyph::FontArc::new)
                        })
                    })
                    .and_then(Result::ok);
                // A missing/unsupported family must not scan the database every frame.
                fonts.insert(family.into(), font);
            }
            fonts.get(family).cloned().flatten()
        };
        let font = custom.as_ref().unwrap_or(&self.font);
        let glyphs: Vec<_> = text
            .chars()
            .take(128)
            .map(|c| self.glyphs.borrow_mut().get(family, c, size, font))
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
/// Composite contiguous RGBA rows without repeated coordinate/buffer checks.
/// Opaque rows are copied; mixed rows use the same Pixel::blend as imageops.
fn overlay_rgba(bottom: &mut RgbaImage, top: &RgbaImage, x: i64, y: i64) {
    overlay_rgba_inner(bottom, top, x, y, false);
}
fn overlay_rgba_inner(bottom: &mut RgbaImage, top: &RgbaImage, x: i64, y: i64, opaque: bool) {
    let dst_x = x.clamp(0, i64::from(bottom.width())) as usize;
    let dst_y = y.clamp(0, i64::from(bottom.height())) as usize;
    let src_x = x.saturating_neg().clamp(0, i64::from(top.width())) as usize;
    let src_y = y.saturating_neg().clamp(0, i64::from(top.height())) as usize;
    let width = (bottom.width() as usize - dst_x).min(top.width() as usize - src_x);
    let height = (bottom.height() as usize - dst_y).min(top.height() as usize - src_y);
    if width == 0 || height == 0 {
        return;
    }
    let dst_stride = bottom.width() as usize * 4;
    let src_stride = top.width() as usize * 4;
    for row in 0..height {
        let start = (src_y + row) * src_stride + src_x * 4;
        let source = &top.as_raw()[start..start + width * 4];
        let start = (dst_y + row) * dst_stride + dst_x * 4;
        let target = &mut bottom.as_mut()[start..start + width * 4];
        if opaque
            || source
                .as_chunks::<4>()
                .0
                .iter()
                .all(|pixel| pixel[3] == 255)
        {
            target.copy_from_slice(source);
        } else {
            for (source, target) in source
                .as_chunks::<4>()
                .0
                .iter()
                .zip(target.as_chunks_mut::<4>().0)
            {
                if source[3] == 255 {
                    target.copy_from_slice(source);
                } else if source[3] != 0 {
                    Rgba::from_slice_mut(target).blend(Rgba::from_slice(source));
                }
            }
        }
    }
}
fn put_pixel(img: &mut RgbaImage, x: i32, y: i32, color: [u8; 4]) {
    if x >= 0 && y >= 0 && (x as u32) < img.width() && (y as u32) < img.height() {
        blend_pixel(img, x as u32, y as u32, color);
    }
}
fn blend_pixel(img: &mut RgbaImage, x: u32, y: u32, color: [u8; 4]) {
    let pixel = img.get_pixel_mut(x, y);
    let alpha = color[3] as u32;
    for i in 0..3 {
        pixel[i] = ((color[i] as u32 * alpha + pixel[i] as u32 * (255 - alpha)) / 255) as u8;
    }
    pixel[3] = 255;
}
fn draw_line(
    img: &mut RgbaImage,
    start: (i32, i32),
    end: (i32, i32),
    color: [u8; 4],
    thickness: i32,
) {
    let steps = (end.0 - start.0).abs().max((end.1 - start.1).abs()).max(1);
    for step in 0..=steps {
        let x = start.0 + (end.0 - start.0) * step / steps;
        let y = start.1 + (end.1 - start.1) * step / steps;
        for dx in -thickness / 2..=thickness / 2 {
            for dy in -thickness / 2..=thickness / 2 {
                put_pixel(img, x + dx, y + dy, color);
            }
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
fn repeatable_media(path: &str, options: &Value) -> bool {
    options["loop"].as_bool() != Some(false)
        || !Path::new(path)
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| {
                ["gif", "mp4", "mkv", "webm", "mov", "avi"]
                    .iter()
                    .any(|format| ext.eq_ignore_ascii_case(format))
            })
}
fn rgb_pixels(img: &RgbaImage) -> Arc<[u8]> {
    crate::pixels::rgb_pixels(img)
}

fn rotate_ccw<I: image::GenericImageView<Pixel = image::Rgb<u8>>>(
    img: &I,
    rotation: u16,
) -> image::RgbImage {
    match rotation % 360 {
        90 => imageops::rotate270(img),
        180 => imageops::rotate180(img),
        270 => imageops::rotate90(img),
        _ => image::RgbImage::from_fn(img.width(), img.height(), |x, y| img.get_pixel(x, y)),
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
    overlay_rgba(&mut canvas, &resized, x, y);
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
    overlay_rgba(&mut canvas, &resized, i64::from(dx), i64::from(dy));
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
        key_size: None,
        max_fps: MAX_FPS,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn single_pass_reuse_is_bounded_and_shared_looping_sources_keep_the_main_cache() {
        // Reserve a window large enough for one native JPEG, then force history eviction.
        let mut renderer = Renderer::new(256 * 1024).unwrap();
        let format = Kind::Mk2.key_image_format();
        for index in 0..64 {
            let image = RgbaImage::from_fn(72, 72, |x, y| {
                Rgba([
                    (x * 11 + index * 13) as u8,
                    (y * 19 + index * 7) as u8,
                    (x * 3 + y * 5 + index * 23) as u8,
                    255,
                ])
            });
            let a = renderer.encode(0, image.clone(), format, 0, false).unwrap();
            let b = renderer.encode(1, image, format, 0, false).unwrap();
            assert!(
                Arc::ptr_eq(&a.encoded, &b.encoded),
                "same-frame keys reuse compression"
            );
            assert!(renderer.streaming_encoded.bytes() <= renderer.streaming_budget);
            assert!(
                renderer.encoded.bytes() + renderer.streaming_encoded.bytes()
                    <= renderer.encoded_budget
            );
        }
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("shared.gif");
        let mut encoder =
            gif::Encoder::new(std::fs::File::create(&path).unwrap(), 72, 72, &[]).unwrap();
        for color in [[220, 40, 10, 255], [10, 200, 50, 255]] {
            let mut pixels = color.repeat(72 * 72);
            let mut frame = gif::Frame::from_rgba_speed(72, 72, &mut pixels, 10);
            frame.delay = 100;
            encoder.write_frame(&frame).unwrap();
        }
        drop(encoder);
        let mut config = default_config(json!({"name":"Mixed"}));
        config.page["keys"] = json!({
            "0x0":{"states":{"0":{"media":{"path":path,"loop":false}}}},
            "1x0":{"states":{"0":{"media":{"path":path,"loop":true}}}}
        });
        let frame = renderer.render(Kind::Mk2, &config).unwrap();
        assert_eq!(frame.tiles[0].identity, frame.tiles[1].identity);
        assert!(Arc::ptr_eq(
            &frame.tiles[0].encoded,
            &frame.tiles[1].encoded
        ));
        assert!(
            renderer.encoded.get(&frame.tiles[1].identity).is_some(),
            "a looping instance of the same file must promote the shared encoding"
        );
        assert!(!repeatable_media("video.MKV", &json!({"loop":false})));
        assert!(repeatable_media("video.MKV", &json!({"loop":true})));
        assert!(repeatable_media("image.png", &json!({"loop":false})));
        renderer.release_media();
        assert_eq!(renderer.streaming_encoded.bytes(), 0);
        assert_eq!(renderer.encoded.bytes(), 0);
    }
    #[test]
    fn row_compositing_matches_reference_for_alpha_clipping_and_extreme_offsets() {
        let offsets = [i64::MIN, -31, -3, -1, 0, 1, 5, 29, i64::MAX];
        for (width, height) in [(0, 0), (1, 1), (13, 7), (31, 17)] {
            let mut top = RgbaImage::new(width, height);
            for (x, y, pixel) in top.enumerate_pixels_mut() {
                *pixel = Rgba([
                    (x * 19 + y * 23) as u8,
                    (x * 31 + y * 7) as u8,
                    (x * 5 + y * 41) as u8,
                    if y % 3 == 0 {
                        255
                    } else {
                        (x * 37 + y * 51) as u8
                    },
                ]);
            }
            for x in offsets {
                for y in offsets {
                    let mut expected = RgbaImage::from_fn(17, 11, |x, y| {
                        Rgba([x as u8 * 11, y as u8 * 19, 73, (x * 13 + y * 17) as u8])
                    });
                    let mut actual = expected.clone();
                    imageops::overlay(&mut expected, &top, x, y);
                    overlay_rgba(&mut actual, &top, x, y);
                    assert_eq!(actual, expected, "top {width}x{height}, offset {x},{y}");
                }
            }
        }
    }
    #[test]
    fn cached_opacity_composition_matches_reference_and_rechecks_changed_images() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        for alpha in [255, 0, 127, 254] {
            let mut top = Arc::new(RgbaImage::from_fn(19, 13, |x, y| {
                Rgba([x as u8 * 7, y as u8 * 11, 91, alpha])
            }));
            for (x, y) in [(0, 0), (4, 7), (-7, -4), (i64::MIN, i64::MAX)] {
                let mut actual = RgbaImage::from_pixel(17, 23, Rgba([10, 20, 30, 45]));
                let mut expected = actual.clone();
                imageops::overlay(&mut expected, top.as_ref(), x, y);
                renderer.overlay_image(&mut actual, &top, x, y);
                assert_eq!(actual, expected);
            }
            let replacement = Arc::make_mut(&mut top);
            replacement.put_pixel(3, 5, Rgba([100, 101, 102, 127]));
            let mut actual = RgbaImage::from_pixel(19, 13, Rgba([5, 6, 7, 31]));
            let mut expected = actual.clone();
            imageops::overlay(&mut expected, top.as_ref(), 0, 0);
            renderer.overlay_image(&mut actual, &top, 0, 0);
            assert_eq!(actual, expected);
        }
        renderer.release_media();
    }
    #[test]
    fn direct_rgb_conversion_preserves_channels_and_alpha_behavior_in_each_color_space() {
        for space in [
            image::metadata::Cicp::SRGB,
            image::metadata::Cicp::DISPLAY_P3,
        ] {
            let mut image = RgbaImage::from_fn(256, 4, |x, y| {
                Rgba([x as u8, (x * 7 + y * 23) as u8, 251 - y as u8, x as u8])
            });
            image.set_color_space(space).unwrap();
            let expected = DynamicImage::ImageRgba8(image.clone()).to_rgb8();
            let actual = rgb_pixels(&image);
            assert_eq!(actual.as_ref(), expected.as_raw().as_slice());
        }
        assert!(rgb_pixels(&RgbaImage::new(0, 0)).is_empty());
    }
    #[test]
    fn pixel_cache_respects_encoding_and_orientation_and_releases_inactive_bytes() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        let pixels = RgbaImage::from_fn(72, 72, |x, y| Rgba([x as u8 * 3, y as u8 * 3, 90, 255]));
        let format = Kind::Mk2.key_image_format();
        let first = renderer.encode(0, pixels.clone(), format, 0, true).unwrap();
        let repeated = renderer.encode(0, pixels.clone(), format, 0, true).unwrap();
        assert!(Arc::ptr_eq(&first.encoded, &repeated.encoded));
        let mut other_format = format;
        other_format.mirror = ImageMirroring::None;
        let other = renderer
            .encode(0, pixels.clone(), other_format, 0, true)
            .unwrap();
        assert_ne!(first.identity, other.identity);
        let rotated = renderer.encode(0, pixels, format, 90, true).unwrap();
        assert_ne!(first.identity, rotated.identity);
        assert!(renderer.encoded.bytes() > 0);
        renderer.release_media();
        assert_eq!(renderer.encoded.bytes(), 0);
        assert_eq!(renderer.encoded.len(), 0);
        assert!(
            image::load_from_memory(&first.encoded).is_ok(),
            "live frames survive cache eviction"
        );
        let mut target = RgbaImage::new(24, 24);
        renderer.text_font(
            &mut target,
            ("test", "Deckard definitely missing font"),
            14.0,
            [255; 4],
            (0.5, 0.5),
        );
        assert!(
            renderer
                .fonts
                .borrow()
                .get("Deckard definitely missing font")
                .is_some_and(Option::is_none)
        );
    }
    #[test]
    fn endpoint_sized_pixels_are_borrowed_and_oversized_artwork_grids_are_bounded() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        let image = Arc::new(RgbaImage::from_pixel(120, 120, Rgba([10, 20, 30, 127])));
        let scaled = renderer.scaled_media(&image, 120, 120, &Value::Null, "cover");
        assert!(
            Arc::ptr_eq(&image, &scaled),
            "native-sized media requires no intermediate pixels"
        );
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("art.png");
        image.save(&path).unwrap();
        let mut target = RgbaImage::new(120, 120);
        renderer.native_visual(
            &mut target,
            &json!({"artwork":path,"crop":[0,0,64,64],"spacing":[512,512]}),
        );
        assert_eq!(
            renderer.errors,
            vec!["Artwork grid exceeds the pixel budget"]
        );
        assert_eq!(renderer.scaled_bytes, 0);
    }
    #[test]
    fn shared_svg_keeps_endpoint_resolution_and_playback_options_are_independent() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("shared.svg");
        std::fs::write(&path, r#"<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><path d="M0 50H200" stroke="red"/></svg>"#).unwrap();
        let config = default_config(json!({
            "keys":{"0x0":{"states":{"0":{"media":{"path":path}}}}},
            "dials":{"0":{"states":{"0":{"media":{"path":path}}}}}
        }));
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        let frame = renderer.render(Kind::Plus, &config).unwrap();
        assert_eq!(
            renderer.assets.len(),
            2,
            "key and strip SVGs need distinct rasterization sizes"
        );
        assert!(
            renderer
                .assets
                .keys()
                .any(|key| (key.width, key.height) == (200, 100))
        );
        assert_eq!(
            (
                frame.strip.as_ref().unwrap().width,
                frame.strip.as_ref().unwrap().height
            ),
            (800, 100)
        );
        assert!(
            renderer.next_deadline().is_none(),
            "static assets must not create frame timers"
        );
        assert_ne!(
            AssetKey::new("a.gif", 120, 120, &json!({"fps":30}), 120),
            AssetKey::new("a.gif", 120, 120, &json!({"fps":60}), 120)
        );
        assert_ne!(
            AssetKey::new("a.gif", 120, 120, &json!({"loop":false}), 120),
            AssetKey::new("a.gif", 120, 120, &json!({"loop":true}), 120)
        );
    }
    #[test]
    fn negotiated_native_geometry_preserves_all_pixels_after_rotation() {
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        for rotation in [0, 90, 180, 270] {
            let mut config = default_config(json!({}));
            config.rotation = rotation;
            config.key_size = Some((144, 112));
            let frame = renderer.render(Kind::Studio, &config).unwrap();
            let wire = image::load_from_memory(&frame.tiles[0].encoded).unwrap();
            assert_eq!((wire.width(), wire.height()), (144, 112));
            let preview = &frame.tiles[0];
            assert_eq!(
                (preview.width, preview.height),
                if rotation % 180 == 90 {
                    (112, 144)
                } else {
                    (144, 112)
                }
            );
        }
    }
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
        // Use a controlled deadline: encoding time on slower runners must not
        // advance (or loop) the GIF while checking composition reuse.
        let deadline = Instant::now() + std::time::Duration::from_secs(3600);
        if let Asset::Gif(player) = renderer
            .assets
            .values_mut()
            .find(|asset| matches!(asset, Asset::Gif(_)))
            .unwrap()
        {
            player.defer_until(deadline);
        }
        let repeated = renderer.render(Kind::Plus, &config).unwrap();
        assert!(Arc::ptr_eq(&first.tiles[0].rgb, &repeated.tiles[0].rgb));
        if let Asset::Gif(player) = renderer
            .assets
            .values_mut()
            .find(|asset| matches!(asset, Asset::Gif(_)))
            .unwrap()
        {
            player
                .tick(deadline + std::time::Duration::from_millis(1))
                .unwrap();
        } else {
            panic!("GIF must be loaded");
        }
        let changed = renderer.render(Kind::Plus, &config).unwrap();
        assert!(
            Arc::ptr_eq(
                &first.strip.as_ref().unwrap().rgb,
                &changed.strip.as_ref().unwrap().rgb
            ),
            "a static strip must survive key animation without copying"
        );
        assert_ne!(first.tiles[0].identity, changed.tiles[0].identity);
        assert_eq!(&changed.tiles[0].rgb[..3], &[0, 255, 0]);
        config.sleeping = true;
        let sleeping = renderer.render(Kind::Plus, &config).unwrap();
        assert!(sleeping.tiles[0].rgb.iter().all(|byte| *byte == 0));
        renderer.release_media();
        assert!(renderer.previous_frame.is_none());
    }
    #[test]
    fn glyph_cache_is_bounded_and_preserves_descenders_and_spaces() {
        let font = ab_glyph::FontArc::try_from_slice(include_bytes!(
            "../../../Assets/Fonts/Roboto-Regular.ttf"
        ))
        .unwrap();
        let mut cache = GlyphCache::default();
        let g = cache.get("Roboto", 'g', 30.0, &font);
        assert!(g.0.ymin < 0 && g.1.iter().any(|v| *v > 0));
        let space = cache.get("Roboto", ' ', 30.0, &font);
        assert!(space.0.advance_width > 0.0 && space.1.is_empty());
        for size in 6..=72 {
            for c in ' '..='~' {
                cache.get("Roboto", c, size as f32, &font);
            }
        }
        assert!(cache.bytes <= 2 * 1024 * 1024 && cache.count <= 4096);
        let retained = cache.get("Roboto", 'g', 30.0, &font);
        assert_eq!(g.1.as_ref(), retained.1.as_ref());
    }
    #[test]
    fn grid_artwork_resizes_once_and_crops_across_physical_gaps() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("cover.png");
        let mut source = RgbaImage::from_pixel(138, 60, Rgba([255, 0, 0, 255]));
        for y in 0..60 {
            for x in 69..138 {
                source.put_pixel(x, y, Rgba([0, 255, 0, 255]));
            }
        }
        source.save(&path).unwrap();
        let mut renderer = Renderer::new(1024 * 1024).unwrap();
        let mut left = RgbaImage::new(120, 120);
        let mut right = left.clone();
        renderer.native_visual(
            &mut left,
            &json!({"artwork":path,"crop":[0,0,2,1],"spacing":[36,36],"fit":"stretch"}),
        );
        renderer.native_visual(
            &mut right,
            &json!({"artwork":path,"crop":[1,0,2,1],"spacing":[36,36],"fit":"stretch"}),
        );
        assert_eq!(left.get_pixel(119, 60).0, [255, 0, 0, 255]);
        assert_eq!(right.get_pixel(0, 60).0, [0, 255, 0, 255]);
        assert_eq!(
            renderer.scaled.len(),
            1,
            "a whole-grid resize is shared by every crop"
        );
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
            (Kind::Studio, (144, 112)),
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
