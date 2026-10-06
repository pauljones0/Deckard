//! Native implementation of the fork's token-protected remote deck protocol.
use crate::engine::{InputEvent, Shared};
use anyhow::{Context, Result, ensure};
use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::SyncSender,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
const MAX_BODY: usize = 64 * 1024;
const MAX_HEADER: usize = 32 * 1024;
pub(crate) struct Server {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    token_path: PathBuf,
}
struct State {
    shared: Shared,
    events: SyncSender<InputEvent>,
    token: String,
    stop: Arc<AtomicBool>,
    images: Mutex<HashMap<u8, (u64, Value)>>,
}
impl Server {
    pub(crate) fn start(shared: Shared, events: SyncSender<InputEvent>) -> Result<Self> {
        let host = if std::env::var("DECKARD_REMOTE_DECK_LAN").as_deref() == Ok("1") {
            "0.0.0.0"
        } else {
            "127.0.0.1"
        };
        let listener =
            TcpListener::bind((host, 8765)).context("Remote Decks: port 8765 is unavailable")?;
        listener.set_nonblocking(true)?;
        let token_path = shared.lock().unwrap().docs.root.join("remote_deck_token");
        let mut entropy = [0; 32];
        std::fs::File::open("/dev/urandom")?.read_exact(&mut entropy)?;
        let token = URL_SAFE_NO_PAD.encode(entropy);
        crate::persistence::atomic_write_private(&token_path, token.as_bytes())?;
        let stop = Arc::new(AtomicBool::new(false));
        let state = Arc::new(State {
            shared,
            events,
            token,
            stop: stop.clone(),
            images: Mutex::new(HashMap::new()),
        });
        let worker = thread::spawn(move || {
            let mut clients = Vec::<JoinHandle<()>>::new();
            while !state.stop.load(Ordering::Relaxed) {
                let mut index = 0;
                while index < clients.len() {
                    if clients[index].is_finished() {
                        let _ = clients.swap_remove(index).join();
                    } else {
                        index += 1;
                    }
                }
                match listener.accept() {
                    Ok((stream, _)) if clients.len() < 8 => {
                        let state = state.clone();
                        clients.push(thread::spawn(move || {
                            let _ = serve(stream, &state);
                        }));
                    }
                    Ok((mut stream, _)) => {
                        let _ = response(&mut stream, 503, json!({"error":"Server busy"}));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(20))
                    }
                    Err(_) => break,
                }
            }
            for client in clients {
                let _ = client.join();
            }
        });
        Ok(Self {
            stop,
            thread: Some(worker),
            token_path,
        })
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(worker) = self.thread.take() {
            let _ = worker.join();
        }
        let _ = std::fs::remove_file(&self.token_path);
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn datetime(format: &str) -> String {
    let seconds = now() as libc::time_t;
    let mut time = std::mem::MaybeUninit::<libc::tm>::uninit();
    let mut buffer = [0 as libc::c_char; 64];
    let format = std::ffi::CString::new(format).unwrap();
    unsafe {
        if libc::localtime_r(&seconds, time.as_mut_ptr()).is_null() {
            return String::new();
        }
        let count = libc::strftime(
            buffer.as_mut_ptr(),
            buffer.len(),
            format.as_ptr(),
            time.as_ptr(),
        );
        String::from_utf8_lossy(std::slice::from_raw_parts(
            buffer.as_ptr().cast::<u8>(),
            count,
        ))
        .into_owned()
    }
}
fn same_token(a: &[u8], b: &[u8]) -> bool {
    let mut difference = a.len() ^ b.len();
    for (index, byte) in b.iter().enumerate() {
        difference |= usize::from(a.get(index).copied().unwrap_or(0) ^ byte);
    }
    difference == 0
}
fn response(stream: &mut TcpStream, status: u16, value: Value) -> Result<()> {
    let bytes = serde_json::to_vec(&value)?;
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        413 => "Payload Too Large",
        503 => "Service Unavailable",
        _ => "Error",
    };
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type, X-Deckard-Token, Authorization\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        bytes.len()
    )?;
    stream.write_all(&bytes)?;
    Ok(())
}
fn read_more(
    stream: &mut TcpStream,
    buffer: &mut Vec<u8>,
    state: &State,
    deadline: Instant,
) -> Result<()> {
    ensure!(
        !state.stop.load(Ordering::Relaxed) && Instant::now() < deadline,
        "Remote request timed out"
    );
    let mut bytes = [0; 4096];
    match stream.read(&mut bytes) {
        Ok(0) => anyhow::bail!("Remote peer disconnected"),
        Ok(length) => buffer.extend_from_slice(&bytes[..length]),
        Err(e)
            if [std::io::ErrorKind::WouldBlock, std::io::ErrorKind::TimedOut]
                .contains(&e.kind()) => {}
        Err(e) => return Err(e.into()),
    }
    Ok(())
}
fn serve(mut stream: TcpStream, state: &State) -> Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(100)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut buffer = Vec::new();
    let header_end = loop {
        if let Some(end) = buffer.windows(4).position(|s| s == b"\r\n\r\n") {
            break end + 4;
        }
        if buffer.len() > MAX_HEADER {
            return response(&mut stream, 413, json!({"error":"Headers too large"}));
        }
        read_more(&mut stream, &mut buffer, state, deadline)?;
    };
    let header = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
    let mut lines = header.lines();
    let request = lines
        .next()
        .unwrap_or_default()
        .split_whitespace()
        .collect::<Vec<_>>();
    if request.len() != 3 {
        return response(&mut stream, 400, json!({"error":"Invalid request"}));
    }
    let (method, path) = (request[0], request[1]);
    if method == "OPTIONS" {
        return response(&mut stream, 200, json!({}));
    }
    let mut token = "";
    let mut length = None;
    let mut chunked = false;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("X-Deckard-Token") {
                token = value.trim();
            } else if name.eq_ignore_ascii_case("Authorization") && token.is_empty() {
                token = value.trim().strip_prefix("Bearer ").unwrap_or("");
            } else if name.eq_ignore_ascii_case("Content-Length") {
                length = value.trim().parse::<usize>().ok();
            } else if name.eq_ignore_ascii_case("Transfer-Encoding") {
                chunked = true;
            }
        }
    }
    if !same_token(token.as_bytes(), state.token.as_bytes()) {
        return response(&mut stream, 401, json!({"error":"Unauthorized"}));
    }
    if chunked {
        return response(&mut stream, 400, json!({"error":"Content-Length required"}));
    }
    if method == "GET" {
        if path == "/status" {
            return response(
                &mut stream,
                200,
                json!({"status":"online","message":"Deckard Remote Decks server is running","timestamp":now(),"datetime":datetime("%Y-%m-%d %H:%M:%S")}),
            );
        }
        if path == "/images" || path.starts_with("/images/") {
            let frame = state
                .shared
                .lock()
                .unwrap()
                .devices
                .get("remote-deck-1")
                .and_then(|d| d.frame.clone());
            let mut cached = state.images.lock().unwrap();
            if let Some(frame) = frame {
                let mut encoder = crate::media::JpegEncoder::new(75).map_err(anyhow::Error::msg)?;
                for tile in &frame.tiles {
                    if cached
                        .get(&tile.key)
                        .is_none_or(|(identity, _)| *identity != tile.identity)
                    {
                        let data = encoder
                            .encode(&tile.rgb, tile.width, tile.height, 0, (false, false))
                            .map_err(anyhow::Error::msg)?;
                        cached.insert(tile.key,(tile.identity,json!({"data":format!("data:image/jpeg;base64,{}", STANDARD.encode(data)),"timestamp":now()})));
                    }
                }
            }
            let images = cached
                .iter()
                .map(|(key, (_, value))| (key.to_string(), value.clone()))
                .collect::<serde_json::Map<_, _>>();
            if path == "/images" {
                return response(
                    &mut stream,
                    200,
                    json!({"status":"ok","images":images,"timestamp":now()}),
                );
            }
            let Ok(key) = path[8..].parse::<u8>() else {
                return response(&mut stream, 400, json!({"error":"Invalid button ID"}));
            };
            return match images.get(&key.to_string()) {
                Some(image) => response(
                    &mut stream,
                    200,
                    json!({"status":"ok","button_id":key,"image":image,"timestamp":now()}),
                ),
                None => response(
                    &mut stream,
                    404,
                    json!({"error":format!("No image found for button {key}")}),
                ),
            };
        }
    } else if method == "POST" {
        let Some(length) = length else {
            return response(&mut stream, 400, json!({"error":"Invalid Content-Length"}));
        };
        if length > MAX_BODY {
            return response(&mut stream, 413, json!({"error":"Payload too large"}));
        }
        while buffer.len() < header_end + length {
            read_more(&mut stream, &mut buffer, state, deadline)?;
        }
        let Ok(body) = serde_json::from_slice::<Value>(&buffer[header_end..header_end + length])
        else {
            return response(&mut stream, 400, json!({"error":"Invalid JSON"}));
        };
        if path == "/message" {
            return response(
                &mut stream,
                200,
                json!({"status":"success","response":format!("Echo: {} | Received at {}",body["message"].as_str().unwrap_or(""),datetime("%H:%M:%S")),"timestamp":now()}),
            );
        }
        if path == "/button" {
            let (Some(row), Some(col), Some(kind)) = (
                body["row"].as_u64(),
                body["col"].as_u64(),
                body["type"].as_str(),
            ) else {
                return response(&mut stream, 400, json!({"error":"Invalid button event"}));
            };
            if row >= 3 || col >= 5 || !["down", "up"].contains(&kind) {
                return response(&mut stream, 400, json!({"error":"Invalid button event"}));
            }
            if state
                .events
                .try_send(InputEvent {
                    serial: "remote-deck-1".into(),
                    family: "keys".into(),
                    input: format!("{col}x{row}"),
                    event: if kind == "down" { "press" } else { "release" }.into(),
                    value: 0,
                })
                .is_err()
            {
                return response(&mut stream, 503, json!({"error":"Input queue busy"}));
            }
            return response(
                &mut stream,
                200,
                json!({"status":"ok","received":{"type":kind,"row":row,"col":col,"id":row*5+col},"timestamp":now()}),
            );
        }
    }
    response(&mut stream, 404, json!({"error":"Not found"}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn authentication_body_bounds_and_button_delivery() {
        let root = tempfile::tempdir().unwrap();
        let shared = crate::engine::Engine::open(root.path().into()).unwrap();
        let (events, receive) = std::sync::mpsc::sync_channel(4);
        let state = Arc::new(State {
            shared,
            events,
            token: "test-token".into(),
            stop: Arc::new(AtomicBool::new(false)),
            images: Mutex::new(HashMap::new()),
        });
        fn request(state: &Arc<State>, text: &str) -> String {
            let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
            let address = listener.local_addr().unwrap();
            let state = state.clone();
            let worker = thread::spawn(move || {
                serve(listener.accept().unwrap().0, &state).unwrap();
            });
            let mut stream = TcpStream::connect(address).unwrap();
            stream.write_all(text.as_bytes()).unwrap();
            let mut result = String::new();
            stream.read_to_string(&mut result).unwrap();
            worker.join().unwrap();
            result
        }
        assert!(request(&state, "GET /status HTTP/1.1\r\n\r\n").starts_with("HTTP/1.1 401"));
        assert!(request(&state, "OPTIONS /button HTTP/1.1\r\n\r\n").starts_with("HTTP/1.1 200"));
        assert!(request(&state,"POST /button HTTP/1.1\r\nX-Deckard-Token: test-token\r\nContent-Length: 65537\r\n\r\n").starts_with("HTTP/1.1 413"));
        let body = r#"{"type":"down","row":2,"col":4}"#;
        assert!(request(&state,&format!("POST /button HTTP/1.1\r\nAuthorization: Bearer test-token\r\nContent-Length: {}\r\n\r\n{body}",body.len())).starts_with("HTTP/1.1 200"));
        assert_eq!(
            receive.recv_timeout(Duration::from_secs(1)).unwrap().input,
            "4x2"
        );
        assert!(
            request(
                &state,
                "GET /images/no HTTP/1.1\r\nX-Deckard-Token: test-token\r\n\r\n"
            )
            .starts_with("HTTP/1.1 400")
        );
    }
}
