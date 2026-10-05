//! Streaming animations: one GIF canvas and one previous canvas, latest-only video frames.
use anyhow::{Context, Result, ensure};
use image::{Rgba, RgbaImage};
use std::{
    fs::File,
    io::{BufReader, Read},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const MAX_PIXELS: u64 = 4 * 1024 * 1024;
pub struct GifPlayer {
    path: PathBuf,
    reader: gif::Decoder<BufReader<File>>,
    canvas: RgbaImage,
    previous: Option<RgbaImage>,
    dispose: gif::DisposalMethod,
    rect: (u32, u32, u32, u32),
    deadline: Instant,
    pub sequence: u64,
    pub looping: bool,
    pub fps: u32,
    sample: Instant,
    finished: bool,
}
impl GifPlayer {
    fn decoder(path: &Path) -> Result<gif::Decoder<BufReader<File>>> {
        ensure!(
            std::fs::metadata(path)?.len() <= 128 * 1024 * 1024,
            "GIF source too large"
        );
        let mut options = gif::DecodeOptions::new();
        options.set_color_output(gif::ColorOutput::RGBA);
        options.set_memory_limit(gif::MemoryLimit::Bytes(
            std::num::NonZeroU64::new(32 * 1024 * 1024).unwrap(),
        ));
        let decoder = options.read_info(BufReader::new(File::open(path)?))?;
        ensure!(
            u64::from(decoder.width()) * u64::from(decoder.height()) <= MAX_PIXELS,
            "GIF canvas exceeds memory budget"
        );
        Ok(decoder)
    }
    pub fn new(path: &Path) -> Result<Self> {
        let reader = Self::decoder(path)?;
        let canvas = RgbaImage::new(reader.width().into(), reader.height().into());
        Ok(Self {
            path: path.into(),
            reader,
            canvas,
            previous: None,
            dispose: gif::DisposalMethod::Any,
            rect: (0, 0, 0, 0),
            deadline: Instant::now(),
            sequence: 0,
            looping: true,
            fps: 60,
            sample: Instant::now(),
            finished: false,
        })
    }
    pub fn running(&self) -> bool {
        !self.finished
    }
    pub fn memory(&self) -> usize {
        self.canvas.as_raw().len() * 3
    }
    pub fn current_image(&self) -> &RgbaImage {
        &self.canvas
    }
    #[cfg(test)]
    pub(crate) fn defer_until(&mut self, deadline: Instant) {
        self.deadline = deadline;
    }
    pub fn tick(&mut self, now: Instant) -> Result<&RgbaImage> {
        if self.finished || now < self.deadline {
            return Ok(&self.canvas);
        }
        if self.sequence != 0 && now < self.sample {
            return Ok(&self.canvas);
        }
        let interval = Duration::from_micros(1_000_000 / u64::from(self.fps.clamp(1, 60)));
        // Keep the sampling clock anchored: delayed polling must not reduce the FPS cap.
        let phase = now.saturating_duration_since(self.sample).as_micros() % interval.as_micros();
        self.sample = now + interval - Duration::from_micros(phase as u64);
        for _ in 0..8 {
            if self.finished || now < self.deadline {
                break;
            }
            self.advance()?;
        }
        Ok(&self.canvas)
    }
    fn advance(&mut self) -> Result<()> {
        let frame = match self.reader.read_next_frame()? {
            Some(frame) => frame.clone(),
            None if !self.looping => {
                self.finished = true;
                return Ok(());
            }
            None => {
                self.reader = Self::decoder(&self.path)?;
                self.canvas.fill(0);
                self.previous = None;
                self.dispose = gif::DisposalMethod::Any;
                self.reader
                    .read_next_frame()?
                    .context("GIF has no frames")?
                    .clone()
            }
        };
        match self.dispose {
            gif::DisposalMethod::Background => {
                let (x, y, w, h) = self.rect;
                for py in y..(y + h).min(self.canvas.height()) {
                    for px in x..(x + w).min(self.canvas.width()) {
                        self.canvas.put_pixel(px, py, Rgba([0, 0, 0, 0]));
                    }
                }
            }
            gif::DisposalMethod::Previous => {
                if let Some(previous) = self.previous.take() {
                    self.canvas = previous
                }
            }
            _ => {}
        }
        let width = u32::from(frame.width);
        let height = u32::from(frame.height);
        let x = u32::from(frame.left);
        let y = u32::from(frame.top);
        self.previous = if frame.dispose == gif::DisposalMethod::Previous {
            Some(self.canvas.clone())
        } else {
            None
        };
        for py in 0..height {
            for px in 0..width {
                let i = ((py * width + px) * 4) as usize;
                if x + px < self.canvas.width()
                    && y + py < self.canvas.height()
                    && frame.buffer[i + 3] != 0
                {
                    self.canvas
                        .put_pixel(x + px, y + py, Rgba(frame.buffer[i..i + 4].try_into()?));
                }
            }
        }
        self.rect = (x, y, width, height);
        self.dispose = frame.dispose;
        self.deadline += Duration::from_millis(u64::from(frame.delay).max(2) * 10);
        self.sequence = self.sequence.wrapping_add(1);
        Ok(())
    }
}
struct VideoFrame {
    sequence: u64,
    pixels: Arc<RgbaImage>,
}
pub struct VideoPlayer {
    child: Arc<Mutex<Child>>,
    frame: Arc<Mutex<Option<VideoFrame>>>,
    memory: usize,
    reader: Option<std::thread::JoinHandle<()>>,
}
impl VideoPlayer {
    pub fn new(path: &Path, width: u32, height: u32, fps: u32, looping: bool) -> Result<Self> {
        ensure!(
            width > 0 && height > 0 && u64::from(width) * u64::from(height) <= MAX_PIXELS,
            "video dimensions exceed memory budget"
        );
        let executable = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.join("ffmpeg")))
            .filter(|p| p.exists())
            .unwrap_or_else(|| PathBuf::from("ffmpeg"));
        let mut child = Command::new(executable)
            .args([
                "-nostdin",
                "-loglevel",
                "error",
                "-re",
                "-stream_loop",
                if looping { "-1" } else { "0" },
                "-threads",
                "2",
                "-filter_threads",
                "1",
                "-i",
            ])
            .arg(path)
            .args([
                "-vf",
                &format!("fps={},scale={width}:{height}", fps.clamp(1, 60)),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgba",
                "pipe:1",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("video needs ffmpeg in the bundle or PATH")?;
        let mut output = child.stdout.take().context("video output")?;
        let frame = Arc::new(Mutex::new(None));
        let slot = frame.clone();
        let reader = std::thread::spawn(move || {
            let mut sequence = 0;
            loop {
                let mut bytes = vec![0; (width * height * 4) as usize];
                if output.read_exact(&mut bytes).is_err() {
                    break;
                }
                sequence += 1;
                let Some(pixels) = RgbaImage::from_raw(width, height, bytes) else {
                    break;
                };
                *slot.lock().unwrap_or_else(|p| p.into_inner()) = Some(VideoFrame {
                    sequence,
                    pixels: Arc::new(pixels),
                });
            }
        });
        Ok(Self {
            child: Arc::new(Mutex::new(child)),
            frame,
            memory: (width * height * 8) as usize,
            reader: Some(reader),
        })
    }
    pub fn memory(&self) -> usize {
        self.memory
    }
    pub fn running(&self) -> bool {
        self.child
            .lock()
            .is_ok_and(|mut child| child.try_wait().is_ok_and(|status| status.is_none()))
    }
    pub fn current(&self) -> Option<(u64, Arc<RgbaImage>)> {
        self.frame
            .lock()
            .ok()?
            .as_ref()
            .map(|v| (v.sequence, v.pixels.clone()))
    }
}
impl Drop for VideoPlayer {
    fn drop(&mut self) {
        if let Ok(mut child) = self.child.lock() {
            let _ = child.kill();
            let _ = child.wait();
        }
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn gif_frame_timing_transparency_and_disposal_are_streamed() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("a.gif");
        {
            let mut out = File::create(&path).unwrap();
            let mut encoder =
                gif::Encoder::new(&mut out, 2, 1, &[0, 0, 0, 255, 0, 0, 0, 255, 0]).unwrap();
            let a = gif::Frame {
                width: 2,
                height: 1,
                delay: 5,
                dispose: gif::DisposalMethod::Keep,
                buffer: vec![1, 1].into(),
                ..Default::default()
            };
            encoder.write_frame(&a).unwrap();
            let b = gif::Frame {
                width: 2,
                height: 1,
                delay: 20,
                transparent: Some(0),
                dispose: gif::DisposalMethod::Previous,
                buffer: vec![0, 2].into(),
                ..Default::default()
            };
            encoder.write_frame(&b).unwrap();
        }
        let mut player = GifPlayer::new(&path).unwrap();
        let now = Instant::now();
        assert_eq!(
            player.tick(now).unwrap().get_pixel(0, 0).0,
            [255, 0, 0, 255]
        );
        assert_eq!(player.sequence, 1);
        player.tick(now + Duration::from_millis(40)).unwrap();
        assert_eq!(player.sequence, 1);
        let second = player.tick(now + Duration::from_millis(55)).unwrap();
        assert_eq!(second.get_pixel(0, 0).0, [255, 0, 0, 255]);
        assert_eq!(second.get_pixel(1, 0).0, [0, 255, 0, 255]);
        player.tick(now + Duration::from_millis(200)).unwrap();
        assert_eq!(player.sequence, 2);
        player.looping = false;
        let retained = player
            .tick(now + Duration::from_millis(500))
            .unwrap()
            .clone();
        assert_eq!(retained.get_pixel(1, 0).0, [0, 255, 0, 255]);
        assert_eq!(
            player.tick(now + Duration::from_secs(2)).unwrap(),
            &retained
        );
    }
}
