//! Native adaptations of StreamController's MIT-licensed Mirabox/Ulanzi protocols.
//! Protocol research: Renato Schmidt and redphx; original SDK: Dean Camera.
//! See COPYING-STREAMCONTROLLER-MIT and DECKARD-PATCHES.md.
use crate::{info::Kind, StreamDeckError, StreamDeckInput};
use hidapi::{DeviceInfo, HidDevice};
use std::{
    io::{Cursor, Write},
    time::{Duration, Instant},
};

const MIRA_KEYS: [u8; 18] = [13, 10, 7, 4, 1, 16, 14, 11, 8, 5, 2, 17, 15, 12, 9, 6, 3, 18];

/// Stable explicit USB serial, with a path-derived fallback for devices without one.
pub fn serial(info: &DeviceInfo) -> String {
    if let Some(serial) = info.serial_number().filter(|s| !s.is_empty()) {
        return serial.into();
    }
    let hash = info.path().to_bytes().iter().fold(0xcbf29ce484222325u64, |hash, b| (hash ^ u64::from(*b)).wrapping_mul(0x100000001b3));
    format!("USB-{:04x}-{:04x}-{hash:016x}", info.vendor_id(), info.product_id())
}
/// Ulanzi first/header and continuation packets, matching the upstream byte layout.
pub fn ulanzi_packets(command: u16, data: &[u8]) -> Vec<Vec<u8>> {
    let mut first = vec![0; 1024];
    first[..2].copy_from_slice(b"||");
    first[2..4].copy_from_slice(&command.to_be_bytes());
    first[4..8].copy_from_slice(&(data.len() as u32).to_le_bytes());
    let count = data.len().min(1016);
    first[8..8 + count].copy_from_slice(&data[..count]);
    let mut packets = vec![first];
    for chunk in data[count..].chunks(1024) {
        let mut packet = vec![0; 1024];
        packet[..chunk.len()].copy_from_slice(chunk);
        packets.push(packet);
    }
    packets
}
/// Mirabox report ID and padded output payload, including its SDK framing byte.
pub fn mirabox_report(data: &[u8]) -> Vec<u8> {
    let mut report = vec![0; 514];
    let count = data.len().min(512);
    report[1..1 + count].copy_from_slice(&data[..count]);
    report
}
/// Decode a Mirabox release-only ACK into the logical button number.
pub fn mirabox_key(data: &[u8]) -> Option<usize> {
    if data.len() < 10 || &data[..8] != b"ACK\0\0OK\0" {
        return None;
    }
    MIRA_KEYS.iter().position(|key| *key == data[9])
}
/// Decode a Ulanzi press/release while ignoring its phantom grid slot.
pub fn ulanzi_key(data: &[u8]) -> Option<(usize, bool)> {
    if data.len() < 12 || &data[..4] != b"||\x01\x01" || data[9] >= 14 {
        return None;
    }
    Some((usize::from(data[9]), data[11] == 1))
}
/// A ZIP whose continuation framing bytes avoid the firmware's 0x00/0x7c corruption.
pub fn ulanzi_zip(key: u8, revision: u64, png: &[u8]) -> Result<Vec<u8>, StreamDeckError> {
    ulanzi_bundle(&[(key, revision, png)])
}
/// Upload all changed keys together, with unique filenames to bypass firmware caching.
pub fn ulanzi_bundle(images: &[(u8, u64, &[u8])]) -> Result<Vec<u8>, StreamDeckError> {
    let mut manifest = serde_json::Map::new();
    for (key, revision, _) in images {
        if *key > 14 {
            return Err(StreamDeckError::InvalidKeyIndex);
        }
        let icon = format!("{key}_{revision}.png");
        manifest.insert(format!("{}_{}", key % 5, key / 5), serde_json::json!({"State":0,"ViewParam":[{"Icon":format!("icons/{icon}")}]}));
    }
    for padding in 0..2000 {
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated).compression_level(Some(1));
        // Store the leading pad: each attempt shifts subsequent entries by exactly one byte.
        if padding != 0 {
            writer.start_file("_pad.txt", zip::write::SimpleFileOptions::default()).map_err(|_| StreamDeckError::BadData)?;
            writer.write_all(&vec![b'x'; padding]).map_err(|_| StreamDeckError::BadData)?;
        }
        writer.start_file("manifest.json", options).map_err(|_| StreamDeckError::BadData)?;
        writer
            .write_all(&serde_json::to_vec(&manifest).map_err(|_| StreamDeckError::BadData)?)
            .map_err(|_| StreamDeckError::BadData)?;
        for (key, revision, png) in images {
            writer.start_file(format!("icons/{key}_{revision}.png"), options).map_err(|_| StreamDeckError::BadData)?;
            writer.write_all(png).map_err(|_| StreamDeckError::BadData)?;
        }
        let data = writer.finish().map_err(|_| StreamDeckError::BadData)?.into_inner();
        if (1016..data.len()).step_by(1024).all(|i| ![0, 0x7c].contains(&data[i])) {
            return Ok(data);
        }
    }
    Err(StreamDeckError::BadData)
}

pub(crate) struct State {
    buttons: Vec<bool>,
    pulse: bool,
    ping: Instant,
    priming: Instant,
    real_image: bool,
    revision: u64,
}
impl State {
    pub(crate) fn new(kind: Kind) -> Self {
        Self {
            buttons: vec![false; kind.key_count() as usize],
            pulse: false,
            ping: Instant::now(),
            priming: Instant::now(),
            real_image: false,
            revision: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos() as u64,
        }
    }
    fn ulanzi_write(device: &HidDevice, command: u16, data: &[u8]) -> Result<(), StreamDeckError> {
        for packet in ulanzi_packets(command, data) {
            device.write(&packet)?;
        }
        Ok(())
    }
    fn mira_write(device: &HidDevice, data: &[u8]) -> Result<(), StreamDeckError> {
        device.write(&mirabox_report(data))?;
        Ok(())
    }
    fn ping(&mut self, device: &HidDevice) -> Result<(), StreamDeckError> {
        Self::ulanzi_write(device, 6, b"2|0|0|00:00:00|0")?;
        self.ping = Instant::now();
        Ok(())
    }
    fn prime(&mut self, device: &HidDevice) -> Result<(), StreamDeckError> {
        use image::ImageEncoder;
        let mut png = Vec::new();
        image::codecs::png::PngEncoder::new(&mut png).write_image(&[0u8; 8 * 8 * 3], 8, 8, image::ColorType::Rgb8.into())?;
        Self::ulanzi_write(device, 1, &ulanzi_zip(14, 0, &png)?)?;
        self.priming = Instant::now();
        Ok(())
    }
    pub(crate) fn initialize(&mut self, kind: Kind, device: &HidDevice) -> Result<(), StreamDeckError> {
        match kind {
            Kind::UlanziD200 => {
                self.ping(device)?;
                self.prime(device)?;
            }
            Kind::Mirabox293s => {
                for command in [&b"CRT\0\0DIS"[..], &b"CRT\0\0CONNECT"[..], &b"CRT\0\0CLE\0\0\0\xff"[..]] {
                    Self::mira_write(device, command)?;
                }
            }
            Kind::Studio => {
                let mut packet = [0; 1024];
                packet[0] = 2;
                device.write(&packet)?;
            }
            _ => {}
        }
        Ok(())
    }
    pub(crate) fn read(&mut self, kind: Kind, device: &HidDevice, timeout: Option<Duration>) -> Result<StreamDeckInput, StreamDeckError> {
        if kind == Kind::Mirabox293s && self.pulse {
            self.pulse = false;
            self.buttons.fill(false);
            return Ok(StreamDeckInput::ButtonStateChange(self.buttons.clone()));
        }
        if kind == Kind::UlanziD200 {
            if self.ping.elapsed() >= Duration::from_secs(1) {
                self.ping(device)?;
            }
            if !self.real_image && self.priming.elapsed() >= Duration::from_secs(1) {
                self.prime(device)?;
            }
        }
        let mut buffer = [0; 1024];
        let count = device.read_timeout(&mut buffer, timeout.unwrap_or(Duration::ZERO).as_millis().min(i32::MAX as u128) as i32)?;
        if count == 0 {
            return Ok(StreamDeckInput::NoData);
        }
        if kind == Kind::Mirabox293s {
            if let Some(key) = mirabox_key(&buffer[..count]) {
                self.buttons.fill(false);
                self.buttons[key] = true;
                self.pulse = true;
                return Ok(StreamDeckInput::ButtonStateChange(self.buttons.clone()));
            }
        } else if let Some((key, pressed)) = ulanzi_key(&buffer[..count]) {
            self.buttons[key] = pressed;
            return Ok(StreamDeckInput::ButtonStateChange(self.buttons.clone()));
        }
        Ok(StreamDeckInput::NoData)
    }
    pub(crate) fn brightness(&mut self, kind: Kind, device: &HidDevice, value: u8) -> Result<(), StreamDeckError> {
        if kind == Kind::UlanziD200 {
            Self::ulanzi_write(device, 10, value.min(100).to_string().as_bytes())
        } else {
            let mut command = b"CRT\0\0LIG\0\0\0\0".to_vec();
            command[10] = value.min(100);
            Self::mira_write(device, &command)
        }
    }
    pub(crate) fn images(&mut self, device: &HidDevice, images: &[(u8, &[u8])]) -> Result<(), StreamDeckError> {
        let mut bundle = Vec::new();
        for (key, image) in images {
            if *key > 14 {
                return Err(StreamDeckError::InvalidKeyIndex);
            }
            if *key == 14 {
                continue;
            }
            self.revision = self.revision.wrapping_add(1);
            bundle.push((*key, self.revision, *image));
        }
        if !bundle.is_empty() {
            Self::ulanzi_write(device, 1, &ulanzi_bundle(&bundle)?)?;
            self.real_image = true;
        }
        Ok(())
    }
    pub(crate) fn image(&mut self, kind: Kind, device: &HidDevice, key: u8, bytes: &[u8]) -> Result<(), StreamDeckError> {
        if key >= kind.key_count() {
            return Err(StreamDeckError::InvalidKeyIndex);
        }
        if kind == Kind::UlanziD200 {
            self.images(device, &[(key, bytes)])
        } else {
            let size = u16::try_from(bytes.len()).map_err(|_| StreamDeckError::BadData)?;
            let mut command = b"CRT\0\0BAT\0\0".to_vec();
            command.extend(size.to_be_bytes());
            command.push(MIRA_KEYS[usize::from(key)]);
            Self::mira_write(device, &command)?;
            for chunk in bytes.chunks(512) {
                Self::mira_write(device, chunk)?;
            }
            Self::mira_write(device, b"CRT\0\0STP")
        }
    }
}
