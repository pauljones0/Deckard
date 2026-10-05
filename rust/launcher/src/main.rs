mod cli;
mod tray;
mod ui;
use anyhow::{Context, Result, ensure};
use clap::Parser;
use deckard_core::{
    engine::{Engine, Runtime},
    ipc::{self, Instance},
};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
fn data_path(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(deckard_core::persistence::resolve(&path)?);
    }
    let home = PathBuf::from(std::env::var_os("HOME").context("HOME is unset; use --data")?);
    let root = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"))
        .join("deckard");
    if !root.exists() {
        for previous in [
            home.join(".var/app/io.github.nazbert.Deckard/data"),
            home.join(".var/app/com.core447.StreamController/data"),
        ] {
            if previous.join("pages").exists() {
                let parent = root.parent().context("data directory parent")?;
                std::fs::create_dir_all(parent)?;
                let staged = tempfile::Builder::new()
                    .prefix(".deckard-migrate-")
                    .tempdir_in(parent)?;
                copy_tree(&previous, staged.path())?;
                std::fs::rename(staged.path(), &root)?;
                break;
            }
        }
    }
    Ok(deckard_core::persistence::resolve(&root)?)
}
fn copy_tree(source: &Path, target: &Path) -> Result<()> {
    std::fs::create_dir_all(target)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        if ty.is_dir() {
            copy_tree(&entry.path(), &target.join(entry.file_name()))?
        } else if ty.is_file() {
            std::fs::copy(entry.path(), target.join(entry.file_name()))?;
        }
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!(
            "Deckard: {}",
            deckard_core::engine::redact(&format!("{error:#}"))
        );
        std::process::exit(1)
    }
}
fn run() -> Result<()> {
    let args = cli::Args::parse();
    if args.doctor {
        let bytes = deckard_core::media::encode_rgb(&[16, 32, 64], 1, 1, 0, (false, false), 90)
            .map_err(anyhow::Error::msg)?;
        ensure!(
            bytes.starts_with(&[0xff, 0xd8]),
            "native JPEG self-test failed"
        );
        println!(
            "{}",
            json!({"version":env!("CARGO_PKG_VERSION"),"runtime":"Rust","python":false,"plugin_api":1,"jpeg":"libjpeg-turbo SIMD","architecture":std::env::consts::ARCH})
        );
        return Ok(());
    }
    let root = data_path(args.data.clone())?;
    let requests = args.requests()?;
    let Some(instance) = Instance::claim(&root)? else {
        if requests.is_empty() {
            ipc::send(&root, &json!({"method":"show"}))?;
        } else {
            for request in requests {
                let value = ipc::send(&root, &request)?;
                println!("{}", serde_json::to_string_pretty(&value)?);
            }
        }
        return Ok(());
    };
    if args.close_running {
        return Ok(());
    }
    let shared = Engine::open(root.clone())?;
    if !requests.is_empty() {
        for request in requests {
            ensure!(
                ![
                    "change-page",
                    "change-state",
                    "set-brightness",
                    "get-brightness",
                    "emulate-input",
                    "sleep",
                    "wake"
                ]
                .contains(&request["method"].as_str().unwrap_or("")),
                "this command needs a running instance"
            );
            let value = shared.lock().unwrap().command(&request)?;
            println!("{}", serde_json::to_string_pretty(&value)?);
        }
        return Ok(());
    }
    let fakes = args
        .fake_deck_model
        .iter()
        .map(|name| deckard_core::engine::fake_kind(name))
        .collect::<Result<Vec<_>>>()?;
    let runtime = Runtime::start(shared.clone(), fakes, !args.skip_load_hardware_decks)?;
    if args.ui_smoke_test {
        let test_shared = shared.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(3));
            test_shared.lock().unwrap().quit = true;
        });
    }
    let tray = if args.smoke_test || args.ui_smoke_test {
        None
    } else {
        tray::start(shared.clone())
    };
    let server = instance.serve(shared.clone(), runtime.events.clone())?;
    let signal = Arc::new(AtomicBool::new(false));
    for signum in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signum, signal.clone())?;
    }
    if args.smoke_test {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let engine = shared.lock().unwrap();
            if engine.devices.values().any(|d| d.frame.is_some()) {
                println!("{}", engine.status());
                break;
            }
            ensure!(
                Instant::now() < deadline,
                "no rendered fake device before deadline; use --fake-deck-model"
            );
            drop(engine);
            std::thread::sleep(Duration::from_millis(25));
        }
    } else {
        let opened = !(args.daemon_only || args.background);
        loop {
            if signal.load(Ordering::Relaxed) || shared.lock().unwrap().quit {
                break;
            }
            let show = {
                let mut engine = shared.lock().unwrap();
                let show = opened || engine.show_window;
                engine.show_window = false;
                show
            };
            if show {
                ui::run(shared.clone(), runtime.events.clone(), signal.clone())?;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    if args.ui_smoke_test {
        ensure!(
            shared
                .lock()
                .unwrap()
                .devices
                .values()
                .any(|d| d.frame.is_some()),
            "GUI smoke test did not receive a rendered deck"
        );
        if let Some(path) = std::env::var_os("DECKARD_UI_CAPTURE") {
            ensure!(
                PathBuf::from(path).is_file(),
                "GUI screenshot was not captured"
            );
        }
    }
    let restart = shared.lock().unwrap().restart;
    drop(tray);
    drop(server);
    drop(runtime);
    drop(instance);
    if restart {
        use std::os::unix::process::CommandExt;
        let exe = std::env::var_os("APPIMAGE")
            .map(PathBuf::from)
            .unwrap_or(std::env::current_exe()?);
        let error = std::process::Command::new(exe)
            .args(std::env::args_os().skip(1))
            .exec();
        return Err(error.into());
    }
    Ok(())
}
