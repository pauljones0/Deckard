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
    owners: std::collections::HashMap<String, BTreeSet<u16>>,
}
static DEVICE: OnceLock<Mutex<Option<Device>>> = OnceLock::new();
impl Device {
    fn open() -> Result<Self> {
        #[cfg(test)]
        if let Some(path) = std::env::var_os("DECKARD_TEST_INPUT_TRACE") {
            return Ok(Self {
                file: OpenOptions::new().create(true).append(true).open(path)?,
                held: Default::default(),
                owners: Default::default(),
            });
        }
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
            owners: Default::default(),
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
fn with_device<T>(action: impl FnOnce(&mut Device) -> Result<T>) -> Result<T> {
    let mut slot = DEVICE.get_or_init(|| Mutex::new(None)).lock().unwrap();
    if slot.is_none() {
        *slot = Some(Device::open()?);
    }
    action(slot.as_mut().unwrap())
}
fn write_key(owner: &str, code: u16, value: i32) -> Result<()> {
    with_device(|device| {
        if value == 0 {
            device.owners.entry(owner.into()).or_default().remove(&code);
            if device
                .owners
                .iter()
                .any(|(other, keys)| other != owner && keys.contains(&code))
            {
                return Ok(());
            }
        } else {
            device.owners.entry(owner.into()).or_default().insert(code);
        }
        device.event(1, code, value)?;
        device.event(0, 0, 0)
    })
}
pub fn release_owner(owner: &str) {
    if let Some(slot) = DEVICE.get() {
        let mut slot = slot.lock().unwrap();
        if let Some(device) = slot.as_mut()
            && let Some(keys) = device.owners.remove(owner)
        {
            for code in keys {
                if !device.owners.values().any(|keys| keys.contains(&code)) {
                    let _ = device.event(1, code, 0);
                }
            }
            let _ = device.event(0, 0, 0);
        }
    }
}
pub fn execute(operation: &str, settings: &Value) -> Result<()> {
    execute_owned(operation, settings, "one-shot")
}
pub fn execute_owned(operation: &str, settings: &Value, owner: &str) -> Result<()> {
    let delay = settings["delay"].as_f64().unwrap_or(0.02);
    ensure!(
        delay.is_finite() && (0.0..=60.0).contains(&delay),
        "invalid key delay"
    );
    let held = settings["hold_until_release"].as_bool().unwrap_or(false);
    let repeat = settings["repeat"].as_bool().unwrap_or(false);
    let result = (|| {
        match operation {
            "keys" => {
                let keys = sequence(settings)?;
                loop {
                    for &(code, value) in &keys {
                        ensure!(!crate::desktop::cancelled(), "input action cancelled");
                        if held && value != 1 {
                            continue;
                        }
                        write_key(owner, code, value)?;
                        crate::desktop::wait(Duration::from_secs_f64(delay))?;
                    }
                    if !repeat {
                        break;
                    }
                    crate::desktop::wait(Duration::from_millis(1))?;
                }
            }
            "click" => {
                let button = match settings["button"].as_str().unwrap_or("left") {
                    "left" => 272,
                    "right" => 273,
                    "middle" => 274,
                    _ => anyhow::bail!("invalid mouse button"),
                };
                write_key(owner, button, 1)?;
                crate::desktop::wait(Duration::from_millis(10))?;
                write_key(owner, button, 0)?;
            }
            "move" | "position" => {
                let x = settings["x"].as_i64().unwrap_or(0);
                let y = settings["y"].as_i64().unwrap_or(0);
                ensure!(
                    (-32768..=32767).contains(&x) && (-32768..=32767).contains(&y),
                    "mouse coordinates outside supported range"
                );
                with_device(|device| {
                    if operation == "position" {
                        device.event(2, 0, -25000)?;
                        device.event(2, 1, -25000)?;
                        device.event(0, 0, 0)?;
                    }
                    device.event(2, 0, x as i32)?;
                    device.event(2, 1, y as i32)?;
                    device.event(0, 0, 0)
                })?;
            }
            _ => anyhow::bail!("unsupported native input operation"),
        }
        Ok(())
    })();
    if !held || result.is_err() || crate::desktop::cancelled() {
        release_owner(owner);
    }
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
    fn input_lifecycle_emits_release_on_release_page_switch_and_shutdown() {
        let tmp = tempfile::tempdir().unwrap();
        let trace = tmp.path().join("input.bin");
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "uinput::tests::lifecycle_child", "--nocapture"])
            .env("DECKARD_TEST_INPUT_TRACE", &trace)
            .status()
            .unwrap();
        assert!(status.success());
        let bytes = std::fs::read(trace).unwrap();
        let mut held = std::collections::BTreeSet::new();
        let mut downs = 0;
        for event in bytes.as_chunks::<24>().0 {
            let kind = u16::from_ne_bytes(event[16..18].try_into().unwrap());
            let code = u16::from_ne_bytes(event[18..20].try_into().unwrap());
            let value = i32::from_ne_bytes(event[20..24].try_into().unwrap());
            if kind == 1 {
                if value == 0 {
                    held.remove(&code);
                } else {
                    held.insert(code);
                    downs += 1;
                }
            }
        }
        assert!(downs >= 6, "held and repeating presses were not exercised");
        assert!(held.is_empty(), "keys left held: {held:?}");
    }
    #[test]
    fn lifecycle_child() {
        if std::env::var_os("DECKARD_TEST_INPUT_TRACE").is_none() {
            return;
        }
        use crate::engine::{Engine, InputEvent, Runtime};
        use elgato_streamdeck::info::Kind;
        use serde_json::json;
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        {
            let mut e = shared.lock().unwrap();
            e.docs.put("Main",json!({"keys":{"0x0":{"states":{"0":{"actions":[{"id":"native::OSPlugin-Hotkey","event":"auto","settings":{"keys":[[29,1],[30,1],[30,0],[29,0]],"hold_until_release":true,"delay":0}}]}}},"1x0":{"states":{"0":{"actions":[{"id":"native::OSPlugin-Hotkey","event":"auto","settings":{"keys":[[30,1],[30,0]],"repeat":true,"delay":0.005}}]}}}}})).unwrap();
            e.docs.create("Other").unwrap();
        }
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while shared.lock().unwrap().devices.is_empty() {
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        let send = |input: &str, event: &str| {
            runtime
                .events
                .send(InputEvent {
                    serial: "FAKE-PLUS-0".into(),
                    family: "keys".into(),
                    input: input.into(),
                    event: event.into(),
                    value: 1,
                })
                .unwrap()
        };
        send("0x0", "press");
        std::thread::sleep(Duration::from_millis(100));
        assert!(
            DEVICE
                .get()
                .unwrap()
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .held
                .contains(&29)
        );
        send("0x0", "release");
        std::thread::sleep(Duration::from_millis(60));
        assert!(
            DEVICE
                .get()
                .unwrap()
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .held
                .is_empty()
        );
        send("1x0", "press");
        std::thread::sleep(Duration::from_millis(100));
        send("1x0", "release");
        std::thread::sleep(Duration::from_millis(60));
        assert!(
            DEVICE
                .get()
                .unwrap()
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .held
                .is_empty()
        );
        send("0x0", "press");
        std::thread::sleep(Duration::from_millis(60));
        shared
            .lock()
            .unwrap()
            .command(
                &json!({"method":"change-page","params":{"serial":"FAKE-PLUS-0","page":"Other"}}),
            )
            .unwrap();
        std::thread::sleep(Duration::from_millis(60));
        assert!(
            DEVICE
                .get()
                .unwrap()
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .held
                .is_empty()
        );
        shared
            .lock()
            .unwrap()
            .command(
                &json!({"method":"change-page","params":{"serial":"FAKE-PLUS-0","page":"Main"}}),
            )
            .unwrap();
        send("0x0", "press");
        std::thread::sleep(Duration::from_millis(60));
        drop(runtime);
        assert!(DEVICE.get().unwrap().lock().unwrap().is_none());
    }
    #[test]
    fn rejects_invalid_kernel_events_before_opening_device() {
        assert!(sequence(&serde_json::json!({"keys":[[42,1],[30,1],[30,0],[42,0]]})).is_ok());
        assert!(sequence(&serde_json::json!({"keys":[[768,1]]})).is_err());
        assert!(sequence(&serde_json::json!({"keys":[[30,3]]})).is_err());
    }
}
