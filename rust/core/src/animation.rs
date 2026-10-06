//! Streaming animations: one GIF canvas and one previous canvas, latest-only video frames.
use anyhow::{Context, Result, ensure};
use image::{Rgba, RgbaImage};
use std::{
    fs::File,
    io::{BufReader, Read},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

pub const MAX_FPS: u32 = 120;
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
            fps: 100,
            sample: Instant::now(),
            finished: false,
        })
    }
    pub fn running(&self) -> bool {
        !self.finished
    }
    pub fn next_deadline(&self) -> Option<Instant> {
        self.running().then_some(self.deadline.max(self.sample))
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
        let interval = Duration::from_micros(1_000_000 / u64::from(self.fps.clamp(1, MAX_FPS)));
        // Keep the sampling clock anchored: delayed polling must not reduce the FPS cap.
        let phase = now.saturating_duration_since(self.sample).as_micros() % interval.as_micros();
        self.sample = now + interval - Duration::from_micros(phase as u64);
        let started = Instant::now();
        for index in 0..4096 {
            if self.finished || now < self.deadline {
                break;
            }
            self.advance()?;
            if index % 8 == 7 && started.elapsed() >= Duration::from_millis(5) {
                break;
            }
        }
        // Continue bounded catch-up without waiting for another sampling period.
        // The renderer retains its last complete frame until the timeline catches up.
        if !self.caught_up(now) {
            self.sample = now;
        }
        Ok(&self.canvas)
    }
    pub fn caught_up(&self, now: Instant) -> bool {
        self.finished || self.deadline > now
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
        let visible_width = width.min(self.canvas.width().saturating_sub(x));
        let visible_height = if visible_width == 0 {
            0
        } else {
            height.min(self.canvas.height().saturating_sub(y))
        };
        let stride = self.canvas.width() as usize * 4;
        for py in 0..visible_height as usize {
            let source = &frame.buffer
                [py * width as usize * 4..(py * width as usize + visible_width as usize) * 4];
            let offset = (y as usize + py) * stride + x as usize * 4;
            let target = &mut self.canvas.as_mut()[offset..offset + source.len()];
            if frame.transparent.is_none() {
                target.copy_from_slice(source);
            } else {
                for (source, target) in source
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .zip(target.as_chunks_mut::<4>().0)
                {
                    if source[3] != 0 {
                        target.copy_from_slice(source);
                    }
                }
            }
        }
        self.rect = (x, y, width, height);
        self.dispose = frame.dispose;
        // A one-centisecond delay is valid (100 FPS). Only unspecified zero delays
        // receive a fallback, avoiding a spin without slowing valid fast GIFs.
        self.deadline +=
            Duration::from_millis(u64::from(if frame.delay == 0 { 2 } else { frame.delay }) * 10);
        self.sequence = self.sequence.wrapping_add(1);
        Ok(())
    }
}
struct VideoFrame {
    sequence: u64,
    pixels: Arc<RgbaImage>,
    produced: Instant,
}
pub struct VideoPlayer {
    child: Arc<Mutex<Child>>,
    frame: Arc<Mutex<Option<VideoFrame>>>,
    memory: usize,
    interval: Duration,
    stop: Arc<AtomicBool>,
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
        // Preserve the source rate, including fractional rates, rather than generating
        // duplicate frames at a fixed 30/60 FPS. ffprobe ships beside ffmpeg.
        let probe = executable.with_file_name("ffprobe");
        let source_fps = Command::new(probe)
            .args([
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
            ])
            .arg(path)
            .output()
            .ok()
            .filter(|out| out.status.success())
            .and_then(|out| parse_rate(&String::from_utf8_lossy(&out.stdout)))
            .unwrap_or(60.0);
        let rate = source_fps.min(f64::from(if fps == 0 {
            MAX_FPS
        } else {
            fps.clamp(1, MAX_FPS)
        }));
        let interval = Duration::from_secs_f64(1.0 / rate);
        let mut child = Command::new(executable)
            .args([
                "-nostdin",
                "-loglevel",
                "error",
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
                &format!("fps={},scale={width}:{height}", rate),
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
        let stop = Arc::new(AtomicBool::new(false));
        let reader_stop = stop.clone();
        let reader = std::thread::spawn(move || {
            let mut sequence = 0;
            let mut clock = Instant::now();
            loop {
                let mut bytes = vec![0; (width * height * 4) as usize];
                if output.read_exact(&mut bytes).is_err() {
                    break;
                }
                // Pace the consumer, not a coarse ffmpeg -re input poll. The pipe
                // applies bounded backpressure to decoding and holds no frame history.
                if sequence == 0 {
                    clock = Instant::now();
                } else {
                    clock += interval;
                    while !reader_stop.load(Ordering::Relaxed) && Instant::now() < clock {
                        std::thread::park_timeout(clock.saturating_duration_since(Instant::now()));
                    }
                    if reader_stop.load(Ordering::Relaxed) {
                        break;
                    }
                }
                sequence += 1;
                let Some(pixels) = RgbaImage::from_raw(width, height, bytes) else {
                    break;
                };
                *slot.lock().unwrap_or_else(|p| p.into_inner()) = Some(VideoFrame {
                    sequence,
                    pixels: Arc::new(pixels),
                    produced: Instant::now(),
                });
            }
        });
        Ok(Self {
            child: Arc::new(Mutex::new(child)),
            frame,
            memory: (width * height * 12) as usize,
            interval,
            stop,
            reader: Some(reader),
        })
    }
    pub fn memory(&self) -> usize {
        self.memory
    }
    pub fn running(&self) -> bool {
        if let Ok(mut child) = self.child.lock() {
            let _ = child.try_wait();
        }
        self.reader
            .as_ref()
            .is_some_and(|reader| !reader.is_finished())
    }
    pub fn next_deadline(&self) -> Instant {
        self.frame
            .lock()
            .ok()
            .and_then(|frame| frame.as_ref().map(|f| f.produced + self.interval))
            .unwrap_or_else(|| Instant::now() + self.interval)
            .max(Instant::now() + Duration::from_millis(1))
    }
    pub fn current(&self) -> Option<(u64, Arc<RgbaImage>)> {
        self.frame
            .lock()
            .ok()?
            .as_ref()
            .map(|v| (v.sequence, v.pixels.clone()))
    }
}
fn parse_rate(text: &str) -> Option<f64> {
    let (n, d) = text.trim().split_once('/')?;
    let rate = n.parse::<f64>().ok()? / d.parse::<f64>().ok()?;
    (rate.is_finite() && rate > 0.0 && rate <= 1000.0).then_some(rate)
}
impl Drop for VideoPlayer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(reader) = &self.reader {
            reader.thread().unpark();
        }
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
    fn centisecond_gifs_keep_100_fps_and_low_caps_have_anchored_deadlines() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("fast.gif");
        {
            let mut out = File::create(&path).unwrap();
            let mut encoder = gif::Encoder::new(&mut out, 1, 1, &[0, 0, 0, 255, 255, 255]).unwrap();
            for i in 0..40 {
                encoder
                    .write_frame(&gif::Frame {
                        width: 1,
                        height: 1,
                        delay: 1,
                        buffer: vec![i % 2].into(),
                        ..Default::default()
                    })
                    .unwrap();
            }
        }
        let mut player = GifPlayer::new(&path).unwrap();
        let now = player.deadline.max(player.sample);
        for i in 0..20 {
            player.tick(now + Duration::from_millis(i * 10)).unwrap();
            assert_eq!(
                player.sequence,
                i + 1,
                "valid 10 ms source frames must survive"
            );
        }
        player.tick(now + Duration::from_millis(1000)).unwrap();
        assert_eq!(
            player.sequence, 101,
            "delayed transport must retain the source timeline"
        );
        player.fps = 30;
        let cap_start = player.next_deadline().unwrap();
        player.tick(cap_start).unwrap();
        let deadline = player.next_deadline().unwrap();
        assert!(deadline >= cap_start + Duration::from_millis(20));
        let sequence = player.sequence;
        player.tick(deadline - Duration::from_micros(1)).unwrap();
        assert_eq!(player.sequence, sequence);
        player.tick(deadline).unwrap();
        assert!(player.sequence > sequence);
    }
    #[test]
    fn fractional_video_rate_and_nonlooping_reader_preserve_final_frames() {
        assert!((parse_rate("30000/1001\n").unwrap() - 29.97002997).abs() < 0.00001);
        for invalid in ["0/0", "60/0", "-1/1", "garbage"] {
            assert!(parse_rate(invalid).is_none());
        }
        if Command::new("ffmpeg").arg("-version").output().is_err()
            || Command::new("ffprobe").arg("-version").output().is_err()
        {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("60fps.mkv");
        assert!(
            Command::new("ffmpeg")
                .args([
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=16x16:rate=60",
                    "-frames:v",
                    "12",
                    "-c:v",
                    "ffv1",
                    "-threads",
                    "1"
                ])
                .arg(&path)
                .status()
                .unwrap()
                .success()
        );
        let player = VideoPlayer::new(&path, 16, 16, 0, false).unwrap();
        assert!((player.interval.as_secs_f64() - 1.0 / 60.0).abs() < 0.000001);
        let start = Instant::now();
        while player.running() && start.elapsed() < Duration::from_secs(3) {
            std::thread::sleep(Duration::from_millis(1));
        }
        assert!(!player.running());
        assert_eq!(
            player.current().unwrap().0,
            12,
            "decoder EOF must not stop a paced reader prematurely"
        );
        assert!(start.elapsed() >= Duration::from_millis(175));
    }
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
