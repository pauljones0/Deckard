//! Rule-gated desktop focus and lock detection with bounded shell-free commands.
use crate::engine::Shared;
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    io::Read,
    os::{fd::AsRawFd, unix::process::CommandExt},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

pub fn run_command(argv: &[String], timeout: Duration) -> Result<String> {
    let (program, args) = argv.split_first().context("empty command")?;
    let bundled = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join(program)))
        .filter(|p| {
            !program.contains('/')
                && ["ffmpeg", "xdotool", "wtype", "pactl"].contains(&program.as_str())
                && p.is_file()
        });
    let mut child = Command::new(
        bundled
            .as_deref()
            .unwrap_or_else(|| std::path::Path::new(program)),
    )
    .args(args)
    .process_group(0)
    .stdin(Stdio::null())
    .stdout(Stdio::piped())
    .stderr(Stdio::null())
    .spawn()
    .with_context(|| format!("could not start {program}"))?;
    let mut stdout = child.stdout.take().context("command stdout")?;
    let fd = stdout.as_raw_fd();
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
    }
    let deadline = Instant::now() + timeout;
    let mut output = Vec::new();
    let mut buffer = [0u8; 8192];
    loop {
        loop {
            match stdout.read(&mut buffer) {
                Ok(0) => break,
                Ok(length) => {
                    output.extend_from_slice(&buffer[..length]);
                    if output.len() > 1024 * 1024 {
                        unsafe {
                            libc::kill(-(child.id() as i32), libc::SIGKILL);
                        }
                        let _ = child.kill();
                        let _ = child.wait();
                        bail!("command output exceeds limit")
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(e.into());
                }
            }
        }
        if let Some(status) = child.try_wait()? {
            ensure!(status.success(), "{program} exited with {status}");
            return Ok(String::from_utf8_lossy(&output).trim_end().to_owned());
        }
        if Instant::now() >= deadline {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            bail!("{program} timed out")
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// Preserve explicitly configured legacy shell syntax and detached application launches.
pub fn run_shell(command: &str, detached: bool) -> Result<()> {
    ensure!(!command.trim().is_empty(), "shell command is empty");
    if !detached {
        run_command(
            &["/bin/sh".into(), "-c".into(), command.into()],
            Duration::from_secs(5),
        )?;
        return Ok(());
    }
    launch_argv(&["/bin/sh", "-c", command])
}
pub fn launch(path: &str) -> Result<()> {
    ensure!(!path.is_empty(), "application path is empty");
    let argv = desktop_argv(path)?;
    launch_argv(&argv.iter().map(String::as_str).collect::<Vec<_>>())
}
fn desktop_argv(command: &str) -> Result<Vec<String>> {
    if std::path::Path::new(command).is_file() {
        return Ok(vec![command.into()]);
    }
    let argv = shell_words::split(command)?
        .into_iter()
        .filter(|word| !["%f", "%F", "%u", "%U", "%i", "%c", "%k"].contains(&word.as_str()))
        .map(|word| word.replace("%%", "%"))
        .collect::<Vec<_>>();
    ensure!(!argv.is_empty(), "desktop application command is empty");
    ensure!(
        argv.iter()
            .all(|word| !regex::Regex::new(r"%[fFuUick]").unwrap().is_match(word)),
        "embedded desktop field codes need explicit arguments"
    );
    Ok(argv)
}
fn launch_argv(argv: &[&str]) -> Result<()> {
    use std::sync::atomic::AtomicUsize;
    static ACTIVE: AtomicUsize = AtomicUsize::new(0);
    ACTIVE
        .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
            (n < 32).then_some(n + 1)
        })
        .map_err(|_| anyhow::anyhow!("32 detached launches are still running"))?;
    let child = Command::new(argv[0])
        .args(&argv[1..])
        .current_dir(
            std::env::var_os("HOME")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|| "/".into()),
        )
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn();
    match child {
        Ok(mut child) => {
            std::thread::spawn(move || {
                let _ = child.wait();
                ACTIVE.fetch_sub(1, Ordering::SeqCst);
            });
        }
        Err(error) => {
            ACTIVE.fetch_sub(1, Ordering::SeqCst);
            return Err(error.into());
        }
    }
    Ok(())
}
fn query(args: &[&str]) -> Option<String> {
    run_command(
        &args.iter().map(|s| (*s).into()).collect::<Vec<_>>(),
        Duration::from_millis(300),
    )
    .ok()
}
fn focused_sway(v: &Value) -> Option<(String, String)> {
    if v["focused"].as_bool() == Some(true) {
        let app = v["app_id"]
            .as_str()
            .or_else(|| v["window_properties"]["class"].as_str())?;
        return Some((app.into(), v["name"].as_str().unwrap_or("").into()));
    }
    for key in ["nodes", "floating_nodes"] {
        if let Some(children) = v[key].as_array() {
            for child in children {
                if let Some(found) = focused_sway(child) {
                    return Some(found);
                }
            }
        }
    }
    None
}
pub fn focused_window() -> Option<(String, String)> {
    let desktop = std::env::var("XDG_CURRENT_DESKTOP")
        .unwrap_or_default()
        .to_lowercase();
    if desktop.contains("gnome") {
        let output = query(&[
            "busctl",
            "--user",
            "--json=short",
            "call",
            "org.gnome.Shell",
            "/org/gnome/Shell/Extensions/StreamController",
            "org.gnome.Shell.Extensions.StreamController",
            "GetFocusedWindow",
        ])?;
        let response: Value = serde_json::from_str(&output).ok()?;
        let value: Value = serde_json::from_str(response["data"][0].as_str()?).ok()?;
        return Some((
            value["wm_class"].as_str()?.into(),
            value["title"].as_str().unwrap_or("").into(),
        ));
    }
    if desktop.contains("hyprland") {
        let value: Value =
            serde_json::from_str(&query(&["hyprctl", "activewindow", "-j"])?).ok()?;
        return Some((
            value["class"].as_str()?.into(),
            value["title"].as_str().unwrap_or("").into(),
        ));
    }
    if desktop.contains("mango") {
        let value: Value =
            serde_json::from_str(&query(&["mmsg", "get", "focusing-client"])?).ok()?;
        return Some((
            value["appid"].as_str()?.into(),
            value["title"].as_str().unwrap_or("").into(),
        ));
    }
    if desktop.contains("sway") {
        let value: Value = serde_json::from_str(&query(&["swaymsg", "-t", "get_tree"])?).ok()?;
        return focused_sway(&value);
    }
    if desktop.contains("kde") || desktop.contains("plasma") {
        let id = query(&["kdotool", "getactivewindow"])?;
        let class = query(&["kdotool", "getwindowclassname", &id])?;
        let title = query(&["kdotool", "getwindowname", &id]).unwrap_or_default();
        return Some((class, title));
    }
    if std::env::var("XDG_SESSION_TYPE").is_ok_and(|s| s == "x11") {
        let id = query(&["xdotool", "getactivewindow"])?;
        let class = query(&["xprop", "-id", &id, "WM_CLASS"])?;
        let title = query(&["xdotool", "getwindowname", &id]).unwrap_or_default();
        return Some((class.split('"').nth(3).unwrap_or("").into(), title));
    }
    None
}
fn locked() -> Option<bool> {
    if let Ok(session) = std::env::var("XDG_SESSION_ID") {
        return query(&[
            "loginctl",
            "show-session",
            &session,
            "-p",
            "LockedHint",
            "--value",
        ])
        .map(|s| s == "yes");
    }
    let pid = std::process::id().to_string();
    let output = query(&[
        "busctl",
        "--system",
        "--json=short",
        "call",
        "org.freedesktop.login1",
        "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager",
        "GetSessionByPID",
        "u",
        &pid,
    ])?;
    let response: Value = serde_json::from_str(&output).ok()?;
    let path = response["data"][0].as_str()?;
    let output = query(&[
        "busctl",
        "--system",
        "--json=short",
        "get-property",
        "org.freedesktop.login1",
        path,
        "org.freedesktop.login1.Session",
        "LockedHint",
    ])?;
    let response: Value = serde_json::from_str(&output).ok()?;
    response["data"]
        .as_bool()
        .or_else(|| response["data"][0].as_bool())
}
struct MangoSocket {
    socket: Option<std::os::unix::net::UnixStream>,
    pending: Vec<u8>,
    retry: Instant,
}
impl MangoSocket {
    fn new() -> Self {
        Self {
            socket: None,
            pending: Vec::new(),
            retry: Instant::now(),
        }
    }
    fn poll(&mut self) -> Option<(String, String)> {
        use std::io::Write;
        if self.socket.is_none() && Instant::now() >= self.retry {
            self.retry = Instant::now() + Duration::from_secs(2);
            let path = std::env::var_os("MANGO_INSTANCE_SIGNATURE")
                .map(std::path::PathBuf::from)
                .filter(|p| p.exists())
                .or_else(|| {
                    let runtime = std::env::var_os("XDG_RUNTIME_DIR")?;
                    std::fs::read_dir(runtime)
                        .ok()?
                        .flatten()
                        .map(|e| e.path())
                        .find(|p| {
                            p.file_name().is_some_and(|n| {
                                n.to_string_lossy().starts_with("mango-")
                                    && n.to_string_lossy().ends_with(".sock")
                            })
                        })
                });
            if let Some(path) = path
                && let Ok(mut socket) = std::os::unix::net::UnixStream::connect(path)
            {
                let _ = socket.set_write_timeout(Some(Duration::from_millis(200)));
                if socket.write_all(b"watch focusing-client\n").is_ok()
                    && socket.set_nonblocking(true).is_ok()
                {
                    self.socket = Some(socket);
                    self.pending.clear();
                }
            }
        }
        let socket = self.socket.as_mut()?;
        let mut latest = None;
        let mut buffer = [0u8; 4096];
        loop {
            match socket.read(&mut buffer) {
                Ok(0) => {
                    self.socket = None;
                    break;
                }
                Ok(n) => {
                    self.pending.extend_from_slice(&buffer[..n]);
                    if self.pending.len() > 1024 * 1024 {
                        self.socket = None;
                        self.pending.clear();
                        break;
                    }
                    while let Some(end) = self.pending.iter().position(|b| *b == b'\n') {
                        let line: Vec<_> = self.pending.drain(..=end).collect();
                        if let Ok(value) = serde_json::from_slice::<Value>(&line)
                            && let Some(class) = value["appid"].as_str()
                        {
                            latest =
                                Some((class.into(), value["title"].as_str().unwrap_or("").into()));
                        }
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    self.socket = None;
                    break;
                }
            }
        }
        latest
    }
}

pub fn watch(shared: Shared, stop: Arc<AtomicBool>) {
    let mut last_focus = None;
    let mut auto_pages = std::collections::HashMap::<String, (String, String, bool)>::new();
    let mut tick = 0;
    let mut mango = MangoSocket::new();
    let is_mango = std::env::var("XDG_CURRENT_DESKTOP")
        .unwrap_or_default()
        .to_lowercase()
        .contains("mango");
    while !stop.load(Ordering::Relaxed) {
        let (rules, autolock) = {
            let engine = shared.lock().unwrap_or_else(|p| p.into_inner());
            let mut rules = engine.docs.settings["rules"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            for (name, page) in &engine.docs.pages {
                let auto = &page["settings"]["auto-change"];
                if auto["enable"].as_bool().unwrap_or(false) {
                    let serials = auto["decks"].as_array().cloned().unwrap_or_default();
                    if serials.is_empty() {
                        rules.push(
                            json!({"page":name,"class":auto["wm-class"],"title":auto["title"]}),
                        );
                    } else {
                        for serial in serials {
                            rules.push(json!({"page":name,"serial":serial,"class":auto["wm-class"],"title":auto["title"]}));
                        }
                    }
                }
            }
            (
                rules,
                engine.docs.settings["auto_lock"].as_bool().unwrap_or(true),
            )
        };
        let mango_event = if is_mango && !rules.is_empty() {
            mango.poll()
        } else {
            mango.socket = None;
            None
        };
        if tick % 20 == 0 || mango_event.is_some() {
            if autolock && let Some(locked) = locked() {
                let mut engine = shared.lock().unwrap();
                if engine.locked != locked {
                    engine.locked = locked;
                    engine.generation += 1;
                    for device in engine.devices.values_mut() {
                        device.pressed.clear();
                    }
                }
            }
            if (!rules.is_empty() || !auto_pages.is_empty())
                && let Some(focus) = mango_event.or_else(focused_window)
                && last_focus.as_ref() != Some(&focus)
            {
                last_focus = Some(focus.clone());
                let mut engine = shared.lock().unwrap();
                let serials: Vec<_> = engine.devices.keys().cloned().collect();
                for serial in serials {
                    if let Some((_, auto, _)) = auto_pages.get(&serial)
                        && engine.devices[&serial].page != *auto
                    {
                        auto_pages.remove(&serial);
                    }
                    let matched = rules.iter().find(|rule| {
                        rule["serial"].as_str().is_none_or(|s| s == serial)
                            && engine
                                .docs
                                .pages
                                .contains_key(rule["page"].as_str().unwrap_or(""))
                            && regex::Regex::new(rule["class"].as_str().unwrap_or(".*"))
                                .is_ok_and(|re| re.is_match(&focus.0))
                            && regex::Regex::new(rule["title"].as_str().unwrap_or(".*"))
                                .is_ok_and(|re| re.is_match(&focus.1))
                    });
                    let target = if let Some(rule) = matched {
                        let page = rule["page"].as_str().unwrap().to_owned();
                        let previous = auto_pages
                            .get(&serial)
                            .map(|(old, ..)| old.clone())
                            .unwrap_or_else(|| engine.devices[&serial].page.clone());
                        let stay = rule["stay_on_page"]
                            .as_bool()
                            .or_else(|| {
                                engine.docs.pages[&page]["settings"]["auto-change"]["stay-on-page"]
                                    .as_bool()
                            })
                            .unwrap_or(true);
                        auto_pages.insert(serial.clone(), (previous, page.clone(), stay));
                        Some(page)
                    } else if auto_pages.get(&serial).is_some_and(|(_, _, stay)| !stay) {
                        auto_pages.remove(&serial).map(|(old, ..)| old)
                    } else {
                        None
                    };
                    if let Some(page) = target.filter(|p| engine.docs.pages.contains_key(p)) {
                        let device = engine.devices.get_mut(&serial).unwrap();
                        if device.page != page {
                            device.page = page;
                            device.states.clear();
                            device.pressed.clear();
                            engine.generation += 1;
                        }
                    }
                }
            }
        }
        tick += 1;
        std::thread::sleep(Duration::from_millis(50));
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn desktop_exec_keeps_quoted_arguments_and_removes_empty_file_placeholders() {
        assert_eq!(
            desktop_argv("firefox --new-window 'https://example.com/path with spaces' %U").unwrap(),
            [
                "firefox",
                "--new-window",
                "https://example.com/path with spaces"
            ]
        );
        assert_eq!(
            desktop_argv("printf '%% $(literal)' ").unwrap(),
            ["printf", "% $(literal)"]
        );
        assert!(desktop_argv("app --url=%u").is_err());
    }
    #[test]
    fn process_timeout_is_bounded_and_no_shell_interpolation_occurs() {
        let start = Instant::now();
        assert!(run_command(&["sleep".into(), "5".into()], Duration::from_millis(50)).is_err());
        assert!(start.elapsed() < Duration::from_secs(1));
        assert_eq!(
            run_command(
                &["printf".into(), "%s".into(), "$(touch /tmp/never)".into()],
                Duration::from_secs(1)
            )
            .unwrap(),
            "$(touch /tmp/never)"
        );
    }
}
