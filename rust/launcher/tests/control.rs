//! Exercise the installed command boundary against a separate native daemon.
use serde_json::{Value, json};
use std::{
    process::{Child, Command},
    time::{Duration, Instant},
};
struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn command(root: &std::path::Path, request: Value) -> Value {
    let output = Command::new(env!("CARGO_BIN_EXE_deckard"))
        .arg("--data")
        .arg(root)
        .arg("--rpc")
        .arg(request.to_string())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
#[test]
fn native_daemon_cli_persistence_and_clean_shutdown() {
    let tmp = tempfile::tempdir().unwrap();
    let mut child = Daemon(
        Command::new(env!("CARGO_BIN_EXE_deckard"))
            .args([
                "--daemon-only",
                "--skip-load-hardware-decks",
                "--fake-deck-model",
                "plus",
            ])
            .arg("--data")
            .arg(tmp.path())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(value) = deckard_core::ipc::send(tmp.path(), &json!({"method":"status"}))
            && value["devices"].as_array().is_some_and(|d| !d.is_empty())
        {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    let output = Command::new(env!("CARGO_BIN_EXE_deckard"))
        .arg("--data")
        .arg(tmp.path())
        .arg("--json")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    let serial = value["devices"][0]["serial"].as_str().unwrap();
    assert_eq!(value["runtime"], "Rust");
    command(
        tmp.path(),
        json!({"method":"create-page","params":{"page":"Integration"}}),
    );
    command(
        tmp.path(),
        json!({"method":"set-state","params":{"page":"Integration","input":"0x0","document":{"labels":{"bottom":{"text":"Native"}},"actions":[{"id":"native::state","settings":{"state":1}}]}}}),
    );
    command(
        tmp.path(),
        json!({"method":"add-state","params":{"page":"Integration","input":"0x0"}}),
    );
    command(
        tmp.path(),
        json!({"method":"change-page","params":{"page":"Integration","serial":serial}}),
    );
    command(
        tmp.path(),
        json!({"method":"emulate-input","params":{"serial":serial,"input":"0x0","event":"press"}}),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let page = command(
            tmp.path(),
            json!({"method":"get-page","params":{"page":"Integration"}}),
        );
        if page["keys"]["0x0"]["active-state"] == 1 {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(25));
    }
    command(tmp.path(), json!({"method":"quit"}));
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline, "daemon shutdown hung");
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!tmp.path().join("native-control.sock").exists());
}
