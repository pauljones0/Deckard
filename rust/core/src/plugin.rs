//! Versioned executable JSON-RPC plugins. Plugins never own GUI objects or hardware.
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::{fd::AsRawFd, unix::process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    time::{Duration, Instant},
};

pub const MAX_MESSAGE: usize = 128 * 1024;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub api: u32,
    pub id: String,
    pub name: String,
    pub version: String,
    pub executable: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub source: String,
    pub actions: Vec<Action>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Action {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub fields: Vec<Field>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Field {
    pub key: String,
    pub label: String,
    #[serde(default = "field_type")]
    pub kind: String,
    #[serde(default)]
    pub default: Value,
}
fn field_type() -> String {
    "string".into()
}
#[derive(Clone)]
pub struct Installed {
    pub directory: PathBuf,
    pub manifest: Manifest,
}
pub fn read_manifest(directory: &Path) -> Result<Manifest> {
    let bytes = fs::read(directory.join("manifest.json"))?;
    ensure!(bytes.len() <= MAX_MESSAGE, "manifest too large");
    let manifest: Manifest = serde_json::from_slice(&bytes)?;
    ensure!(manifest.api == 1, "unsupported native plugin API");
    ensure!(
        !manifest.id.is_empty()
            && manifest
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)),
        "invalid plugin ID"
    );
    ensure!(
        !manifest.actions.is_empty() && manifest.actions.len() <= 128,
        "invalid action count"
    );
    let root = directory.canonicalize()?;
    let executable = directory.join(&manifest.executable).canonicalize()?;
    ensure!(
        executable.starts_with(&root),
        "plugin executable escapes directory"
    );
    let mut ids = std::collections::HashSet::new();
    for action in &manifest.actions {
        ensure!(
            !action.id.is_empty() && !action.id.contains("::") && ids.insert(&action.id),
            "invalid or duplicate action ID"
        );
    }
    let mut magic = [0; 4];
    let mut file = std::fs::File::open(&executable)?;
    file.read_exact(&mut magic)?;
    ensure!(
        &magic == b"\x7fELF",
        "plugins must be native Linux executables; scripts and Python plugins are unsupported"
    );
    let mut header = [0u8; 16];
    file.read_exact(&mut header)?;
    let machine = u16::from_le_bytes([header[14], header[15]]);
    ensure!(
        header[0] == 2
            && header[1] == 1
            && match std::env::consts::ARCH {
                "x86_64" => machine == 62,
                "aarch64" => machine == 183,
                _ => false,
            },
        "plugin architecture does not match this Linux build"
    );
    Ok(manifest)
}
pub fn discover(root: &Path) -> Vec<Installed> {
    let mut plugins = Vec::new();
    if let Ok(entries) = fs::read_dir(root) {
        for entry in entries.flatten() {
            if let Ok(manifest) = read_manifest(&entry.path()) {
                plugins.push(Installed {
                    directory: entry.path(),
                    manifest,
                })
            }
        }
    }
    plugins.sort_by(|a, b| {
        a.manifest
            .name
            .to_lowercase()
            .cmp(&b.manifest.name.to_lowercase())
    });
    plugins
}
pub struct Process {
    child: Child,
    input: ChildStdin,
    output: Receiver<Result<Value, String>>,
    next_id: u64,
}
impl Process {
    pub fn start(installed: &Installed) -> Result<Self> {
        let mut child = Command::new(installed.directory.join(&installed.manifest.executable))
            .current_dir(&installed.directory)
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("starting native plugin")?;
        let input = child.stdin.take().context("plugin stdin")?;
        unsafe {
            let fd = input.as_raw_fd();
            let flags = libc::fcntl(fd, libc::F_GETFL);
            libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
        }
        let output = child.stdout.take().context("plugin stdout")?;
        let (send, receive) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut reader = BufReader::new(output);
            loop {
                let mut line = Vec::new();
                let result = reader
                    .by_ref()
                    .take((MAX_MESSAGE + 1) as u64)
                    .read_until(b'\n', &mut line);
                let response = match result {
                    Ok(0) => break,
                    Ok(_) if line.len() > MAX_MESSAGE => Err("plugin message exceeds limit".into()),
                    Ok(_) => serde_json::from_slice(&line).map_err(|e| e.to_string()),
                    Err(e) => Err(e.to_string()),
                };
                let failed = response.is_err();
                if send.send(response).is_err() || failed {
                    break;
                }
            }
        });
        Ok(Self {
            child,
            input,
            output: receive,
            next_id: 0,
        })
    }
    pub fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        self.next_id += 1;
        let id = self.next_id;
        let message =
            serde_json::to_vec(&json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))?;
        ensure!(
            message.len() <= MAX_MESSAGE,
            "request exceeds plugin protocol limit"
        );
        let mut message = message;
        message.push(b'\n');
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut offset = 0;
        while offset < message.len() {
            match self.input.write(&message[offset..]) {
                Ok(0) => anyhow::bail!("plugin closed stdin"),
                Ok(count) => offset += count,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    ensure!(Instant::now() < deadline, "native plugin stdin timed out");
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => return Err(error.into()),
            }
        }
        let response = self
            .output
            .recv_timeout(Duration::from_secs(2))
            .context("native plugin timed out or disconnected")?
            .map_err(anyhow::Error::msg)?;
        ensure!(
            response["jsonrpc"] == "2.0" && response["id"].as_u64() == Some(id),
            "invalid plugin response ID"
        );
        ensure!(response.get("error").is_none(), "plugin reported an error");
        Ok(response["result"].clone())
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        // Each child has its own process group. Kill descendants before waiting, so
        // an inherited stdout descriptor cannot leave a reader thread stranded.
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_rpc_roundtrip_bad_id_and_blocked_stdin_are_bounded() {
        for mode in [0, 1, 2] {
            let tmp = tempfile::tempdir().unwrap();
            let source = tmp.path().join("fixture.c");
            fs::write(&source, r#"#include <stdio.h>
#include <unistd.h>
int main(void) {
#if MODE == 2
sleep(30);
#else
char line[131073]; if (!fgets(line, sizeof line, stdin)) return 1;
printf("{\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"label\":\"Native fixture\"}}\n", MODE == 0 ? 1 : 999); fflush(stdout); sleep(30);
#endif
return 0; }"#).unwrap();
            assert!(
                Command::new("cc")
                    .arg(format!("-DMODE={mode}"))
                    .arg(&source)
                    .arg("-o")
                    .arg(tmp.path().join("plugin"))
                    .status()
                    .unwrap()
                    .success()
            );
            fs::write(tmp.path().join("manifest.json"), serde_json::to_vec(&json!({"api":1,"id":"test","name":"Test","version":"1","executable":"plugin","actions":[{"id":"hello","name":"Hello"}]})).unwrap()).unwrap();
            let installed = Installed {
                directory: tmp.path().into(),
                manifest: read_manifest(tmp.path()).unwrap(),
            };
            let mut process = Process::start(&installed).unwrap();
            let pid = process.child.id();
            let start = Instant::now();
            let response = process.call(
                "event",
                json!({"settings": "x".repeat(if mode == 2 { 100_000 } else { 1 })}),
            );
            if mode == 0 {
                assert_eq!(response.unwrap()["label"], "Native fixture");
            } else {
                assert!(response.is_err());
            }
            assert!(start.elapsed() < Duration::from_secs(3));
            drop(process);
            assert_eq!(
                unsafe { libc::kill(pid as i32, 0) },
                -1,
                "plugin process must be reaped"
            );
        }
    }
    #[test]
    fn scripts_are_not_accepted_as_native_plugins() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("plugin"), b"#!/usr/bin/python3\n").unwrap();
        fs::write(tmp.path().join("manifest.json"),serde_json::to_vec(&json!({"api":1,"id":"test","name":"Test","version":"1","executable":"plugin","actions":[{"id":"hello","name":"Hello"}]})).unwrap()).unwrap();
        assert!(
            read_manifest(tmp.path())
                .unwrap_err()
                .to_string()
                .contains("native Linux")
        );
    }
}
