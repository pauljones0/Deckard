//! Linux uinput for audited evdev key sequences and mouse actions.
use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs::{File, OpenOptions},
    io::Write,
    os::fd::AsRawFd,
    sync::{Mutex, OnceLock},
    time::Duration,
};
struct Device {
    file: File,
    held: BTreeSet<u16>,
}
static DEVICE: OnceLock<Mutex<Option<Device>>> = OnceLock::new();
impl Device {
    fn open() -> Result<Self> {
        let file=OpenOptions::new().write(true).open("/dev/uinput").context("cannot open /dev/uinput; enable native input access using the installation helper, then log in again")?;
        let fd = file.as_raw_fd();
        for event in [1, 2] {
            ensure!(
                unsafe { libc::ioctl(fd, 0x40045564, event) } >= 0,
                "cannot enable uinput event type"
            );
        }
        for code in 0..=767 {
            ensure!(
                unsafe { libc::ioctl(fd, 0x40045565, code) } >= 0,
                "cannot enable uinput key"
            );
        }
        for code in [0, 1] {
            ensure!(
                unsafe { libc::ioctl(fd, 0x40045566, code) } >= 0,
                "cannot enable relative mouse axis"
            );
        }
        // uinput_user_dev from linux/uinput.h: name[80], input_id, ff_effects_max,
        // four ABS_CNT (64) i32 arrays. Linux x86_64/aarch64 share this layout.
        let mut descriptor = [0u8; 1116];
        descriptor[..13].copy_from_slice(b"Deckard Input");
        descriptor[80..82].copy_from_slice(&3u16.to_ne_bytes());
        let mut device = Self {
            file,
            held: BTreeSet::new(),
        };
        device.file.write_all(&descriptor)?;
        ensure!(
            unsafe { libc::ioctl(fd, 0x5501) } >= 0,
            "cannot create native input device"
        );
        std::thread::sleep(Duration::from_millis(150));
        Ok(device)
    }
    fn event(&mut self, kind: u16, code: u16, value: i32) -> Result<()> {
        let mut bytes = [0u8; 24];
        bytes[16..18].copy_from_slice(&kind.to_ne_bytes());
        bytes[18..20].copy_from_slice(&code.to_ne_bytes());
        bytes[20..24].copy_from_slice(&value.to_ne_bytes());
        self.file.write_all(&bytes)?;
        if kind == 1 {
            if value == 0 {
                self.held.remove(&code);
            } else {
                self.held.insert(code);
            }
        }
        Ok(())
    }
    fn release(&mut self) {
        for key in self.held.clone() {
            let _ = self.event(1, key, 0);
        }
        let _ = self.event(0, 0, 0);
    }
}
impl Drop for Device {
    fn drop(&mut self) {
        self.release();
        unsafe { libc::ioctl(self.file.as_raw_fd(), 0x5502) };
    }
}
pub fn sequence(settings: &Value) -> Result<Vec<(u16, i32)>> {
    let keys = settings["keys"]
        .as_array()
        .context("evdev key sequence missing")?;
    ensure!(keys.len() <= 256, "key sequence exceeds 256 events");
    keys.iter()
        .map(|key| {
            let code = key[0].as_u64().context("evdev key code missing")?;
            let value = key[1].as_i64().context("evdev key value missing")?;
            ensure!(
                code <= 767 && (0..=2).contains(&value),
                "invalid evdev key event"
            );
            Ok((code as u16, value as i32))
        })
        .collect()
}
pub fn execute(operation: &str, settings: &Value) -> Result<()> {
    let keys = if operation == "keys" {
        sequence(settings)?
    } else {
        Vec::new()
    };
    let delay = settings["delay"].as_f64().unwrap_or(0.02);
    ensure!(
        delay.is_finite() && (0.0..=1.0).contains(&delay) && delay * keys.len() as f64 <= 5.0,
        "key sequence delay exceeds five seconds"
    );
    let mut device = DEVICE.get_or_init(|| Mutex::new(None)).lock().unwrap();
    if device.is_none() {
        *device = Some(Device::open()?);
    }
    let device = device.as_mut().unwrap();
    let result = (|| {
        match operation {
            "keys" => {
                for (code, value) in keys {
                    device.event(1, code, value)?;
                    device.event(0, 0, 0)?;
                    std::thread::sleep(Duration::from_secs_f64(delay));
                }
            }
            "click" => {
                let button = match settings["button"].as_str().unwrap_or("left") {
                    "left" => 272,
                    "right" => 273,
                    "middle" => 274,
                    _ => anyhow::bail!("invalid mouse button"),
                };
                device.event(1, button, 1)?;
                device.event(0, 0, 0)?;
                std::thread::sleep(Duration::from_millis(10));
                device.event(1, button, 0)?;
                device.event(0, 0, 0)?;
            }
            "move" => {
                for (code, key) in [(0, "x"), (1, "y")] {
                    let value = settings[key].as_i64().unwrap_or(0);
                    ensure!(
                        (-32768..=32767).contains(&value),
                        "mouse movement outside supported range"
                    );
                    device.event(2, code, value as i32)?;
                }
                device.event(0, 0, 0)?;
            }
            _ => anyhow::bail!("unsupported native input operation"),
        };
        Ok(())
    })();
    device.release();
    result
}
pub fn shutdown() {
    if let Some(device) = DEVICE.get() {
        device.lock().unwrap().take();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_invalid_kernel_events_before_opening_device() {
        assert!(sequence(&serde_json::json!({"keys":[[42,1],[30,1],[30,0],[42,0]]})).is_ok());
        assert!(sequence(&serde_json::json!({"keys":[[768,1]]})).is_err());
        assert!(sequence(&serde_json::json!({"keys":[[30,3]]})).is_err());
    }
}
