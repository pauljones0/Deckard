//! Same-user Unix socket control. A file lock is claimed before opening any USB device.
use crate::engine::{InputEvent, Shared};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{DirBuilderExt, PermissionsExt},
            net::{UnixListener, UnixStream},
        },
    },
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::SyncSender,
    },
    thread::JoinHandle,
    time::Duration,
};
pub struct Instance {
    _lock: File,
    pub socket: PathBuf,
}
impl Instance {
    pub fn claim(root: &Path) -> Result<Option<Self>> {
        if !root.exists() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(root)?;
        }
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(root.join(".native-instance.lock"))?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
                return Ok(None);
            }
            return Err(error.into());
        }
        let socket = root.join("native-control.sock");
        ensure!(
            socket.as_os_str().len() < 100,
            "data path is too long for a Unix control socket"
        );
        Ok(Some(Self {
            _lock: lock,
            socket,
        }))
    }
    pub fn serve(&self, shared: Shared, events: SyncSender<InputEvent>) -> Result<Server> {
        if self.socket.exists() {
            fs::remove_file(&self.socket)?
        }
        let listener = UnixListener::bind(&self.socket)?;
        fs::set_permissions(&self.socket, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let stop = Arc::new(AtomicBool::new(false));
        let server_stop = stop.clone();
        let socket = self.socket.clone();
        let thread = std::thread::spawn(move || {
            while !server_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
                        let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
                        let result = (|| -> Result<Value> {
                            let mut bytes = Vec::new();
                            BufReader::new(stream.try_clone()?)
                                .take(1024 * 1024 + 1)
                                .read_until(b'\n', &mut bytes)?;
                            ensure!(bytes.len() <= 1024 * 1024, "request too large");
                            let request: Value = serde_json::from_slice(&bytes)?;
                            if request["method"] == "emulate-input" {
                                let p = &request["params"];
                                let serial = p["serial"].as_str().context("serial missing")?;
                                ensure!(
                                    shared.lock().unwrap().devices.contains_key(serial),
                                    "device not found"
                                );
                                if let Some(page) = p["page"].as_str() {
                                    ensure!(
                                        shared.lock().unwrap().devices[serial].page == page,
                                        "requested page is not active on this device"
                                    );
                                }
                                ensure!(
                                    [
                                        "press",
                                        "release",
                                        "long-press",
                                        "turn-cw",
                                        "turn-ccw",
                                        "touch",
                                        "long-touch",
                                        "swipe-left",
                                        "swipe-right"
                                    ]
                                    .contains(&p["event"].as_str().unwrap_or("press")),
                                    "unknown input event"
                                );
                                events
                                    .try_send(InputEvent {
                                        serial: serial.into(),
                                        family: p["family"].as_str().unwrap_or("keys").into(),
                                        input: p["input"].as_str().context("input missing")?.into(),
                                        event: p["event"].as_str().unwrap_or("press").into(),
                                        value: p["value"].as_i64().unwrap_or(1) as i32,
                                    })
                                    .context("input queue full")?;
                                Ok(json!({"ok":true}))
                            } else {
                                shared.lock().unwrap().command(&request)
                            }
                        })();
                        let response = match result {
                            Ok(v) => json!({"result":v}),
                            Err(e) => json!({"error":e.to_string()}),
                        };
                        let _ = writeln!(stream, "{response}");
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(20))
                    }
                    Err(_) => break,
                }
            }
            let _ = fs::remove_file(socket);
        });
        Ok(Server {
            stop,
            thread: Some(thread),
        })
    }
}
pub struct Server {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
pub fn send(root: &Path, request: &Value) -> Result<Value> {
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let mut stream = loop {
        match UnixStream::connect(root.join("native-control.sock")) {
            Ok(stream) => break stream,
            Err(error)
                if [
                    std::io::ErrorKind::NotFound,
                    std::io::ErrorKind::ConnectionRefused,
                ]
                .contains(&error.kind())
                    && std::time::Instant::now() < deadline =>
            {
                std::thread::sleep(Duration::from_millis(20))
            }
            Err(error) => return Err(error.into()),
        }
    };
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    writeln!(stream, "{request}")?;
    let mut line = Vec::new();
    BufReader::new(stream)
        .take(2 * 1024 * 1024 + 1)
        .read_until(b'\n', &mut line)?;
    ensure!(line.len() <= 2 * 1024 * 1024, "response exceeds limit");
    let value: Value = serde_json::from_slice(&line)?;
    if let Some(error) = value["error"].as_str() {
        anyhow::bail!("{error}")
    }
    Ok(value["result"].clone())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn claim_is_exclusive_and_rpc_roundtrip_works() {
        let tmp = tempfile::tempdir().unwrap();
        let instance = Instance::claim(tmp.path()).unwrap().unwrap();
        assert!(Instance::claim(tmp.path()).unwrap().is_none());
        let shared = crate::engine::Engine::open(tmp.path().into()).unwrap();
        let (events, _) = std::sync::mpsc::sync_channel(1);
        let server = instance.serve(shared, events).unwrap();
        let value = send(tmp.path(), &json!({"method":"list-pages"})).unwrap();
        assert_eq!(value, json!(["Main"]));
        assert!(send(tmp.path(), &json!({"method":"unknown"})).is_err());
        drop(server);
    }
}
