//! Authenticated OBS WebSocket v5 requests with bounded frames and deadlines.
use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    net::{TcpStream, ToSocketAddrs},
    time::{Duration, Instant},
};
use tungstenite::{Message, WebSocket, stream::MaybeTlsStream};
type Socket = WebSocket<MaybeTlsStream<TcpStream>>;
fn receive(socket: &mut Socket, opcode: u64) -> Result<Value> {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        ensure!(Instant::now() < deadline, "OBS response timed out");
        match socket.read()? {
            Message::Text(text) => {
                let message: Value = serde_json::from_str(&text)?;
                if message["op"].as_u64() == Some(opcode) {
                    return Ok(message["d"].clone());
                }
            }
            Message::Close(_) => anyhow::bail!("OBS closed the connection"),
            _ => {}
        }
    }
}
fn authentication(password: &str, salt: &str, challenge: &str) -> String {
    let secret = STANDARD.encode(Sha256::digest(format!("{password}{salt}")));
    STANDARD.encode(Sha256::digest(format!("{secret}{challenge}")))
}
pub fn connect(connection: &Value, subscriptions: u64) -> Result<Client> {
    let host = connection["host"]
        .as_str()
        .or_else(|| connection["ip"].as_str())
        .unwrap_or("localhost");
    let port = connection["port"]
        .as_u64()
        .or_else(|| {
            connection["port"]
                .as_str()
                .and_then(|port| port.parse().ok())
        })
        .unwrap_or(4455);
    ensure!(port > 0 && port <= 65535, "invalid OBS port");
    ensure!(
        !host.is_empty() && !host.contains(['/', '@', '?', '#', ' ']),
        "invalid OBS host"
    );
    let addresses = (host, port as u16).to_socket_addrs()?;
    let mut stream = None;
    for address in addresses.take(4) {
        if let Ok(connected) = TcpStream::connect_timeout(&address, Duration::from_secs(2)) {
            stream = Some(connected);
            break;
        }
    }
    let stream = stream.context("cannot connect to OBS WebSocket; enable it in OBS Tools")?;
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    let url = format!(
        "{}://{}:{port}",
        if connection["tls"].as_bool() == Some(true) {
            "wss"
        } else {
            "ws"
        },
        if host.contains(':') {
            format!("[{host}]")
        } else {
            host.into()
        }
    );
    let config = tungstenite::protocol::WebSocketConfig::default()
        .max_message_size(Some(1024 * 1024))
        .max_frame_size(Some(1024 * 1024));
    let (mut socket, _) = tungstenite::client_tls_with_config(url, stream, Some(config), None)?;
    let hello = receive(&mut socket, 0)?;
    let mut identify = json!({"rpcVersion":1,"eventSubscriptions":subscriptions});
    if hello["authentication"].is_object() {
        let auth = &hello["authentication"];
        identify["authentication"] = json!(authentication(
            connection["password"].as_str().unwrap_or(""),
            auth["salt"].as_str().context("OBS auth salt missing")?,
            auth["challenge"]
                .as_str()
                .context("OBS auth challenge missing")?
        ));
    }
    socket.send(Message::Text(
        json!({"op":1,"d":identify}).to_string().into(),
    ))?;
    receive(&mut socket, 2)?;
    Ok(Client {
        socket,
        events: std::collections::HashMap::new(),
        readout_cache: std::collections::HashMap::new(),
        readout_bytes: 0,
        event_received: std::collections::HashMap::new(),
    })
}

pub struct Client {
    socket: Socket,
    readout_cache: std::collections::HashMap<String, (Instant, Value, usize)>,
    readout_bytes: usize,
    pub events: std::collections::HashMap<String, Value>,
    pub event_received: std::collections::HashMap<String, Instant>,
}
/// Settings choices share one connection; fetching items is limited to the
/// selected scene so a large OBS collection cannot create an unbounded job.
pub fn choices(connection: &Value, id: &str, scene: &str) -> Result<Value> {
    let mut client = connect(connection, 0)?;
    let mut choices = json!({"connection":id});
    for (key, request) in [
        ("scenes", "GetSceneList"),
        ("inputs", "GetInputList"),
        ("collections", "GetSceneCollectionList"),
    ] {
        choices[key] = client.query(request, json!({}))?;
    }
    if !scene.is_empty() {
        if let Ok(items) = client.query("GetSceneItemList", json!({"sceneName":scene})) {
            choices["items"][scene] = items;
        }
        if let Ok(filters) = client.query("GetSourceFilterList", json!({"sourceName":scene})) {
            choices["filters"][scene] = filters;
        }
    }
    Ok(choices)
}
impl Client {
    pub fn query(&mut self, operation: &str, data: Value) -> Result<Value> {
        request_events(
            &mut self.socket,
            operation,
            data,
            &mut self.events,
            &mut self.event_received,
        )
    }
    pub fn readout(&mut self, operation: &str, data: Value) -> Result<Value> {
        let key = format!("{operation}{data}");
        if let Some((stamp, value, _)) = self.readout_cache.get(&key)
            && stamp.elapsed() < Duration::from_millis(500)
        {
            return Ok(value.clone());
        }
        let value = self.query(operation, data)?;
        let bytes = value.to_string().len();
        if let Some((_, _, previous)) = self.readout_cache.remove(&key) {
            self.readout_bytes -= previous;
        }
        if self.readout_bytes + bytes > 4 * 1024 * 1024 || self.readout_cache.len() >= 256 {
            self.readout_cache.clear();
            self.readout_bytes = 0;
        }
        if bytes <= 4 * 1024 * 1024 {
            self.readout_bytes += bytes;
            self.readout_cache
                .insert(key, (Instant::now(), value.clone(), bytes));
        }
        Ok(value)
    }
    pub fn poll(&mut self) -> Result<()> {
        let stream = match self.socket.get_mut() {
            MaybeTlsStream::Plain(s) => s,
            MaybeTlsStream::Rustls(s) => &mut s.sock,
            _ => anyhow::bail!("unknown OBS transport"),
        };
        stream.set_read_timeout(Some(Duration::from_millis(25)))?;
        let deadline = Instant::now() + Duration::from_millis(30);
        while Instant::now() < deadline {
            match self.socket.read() {
                Ok(Message::Text(text)) => {
                    let v: Value = serde_json::from_str(&text)?;
                    if v["op"] == 5
                        && let Some(name) = v["d"]["eventType"].as_str()
                    {
                        self.events.insert(name.into(), v["d"]["eventData"].clone());
                        self.event_received.insert(name.into(), Instant::now());
                        if name != "InputVolumeMeters" {
                            self.readout_cache.clear();
                            self.readout_bytes = 0;
                        }
                    }
                }
                Err(tungstenite::Error::Io(e))
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) =>
                {
                    break;
                }
                Ok(Message::Close(_)) => anyhow::bail!("OBS closed connection"),
                Err(e) => return Err(e.into()),
                _ => {}
            }
        }
        let stream = match self.socket.get_mut() {
            MaybeTlsStream::Plain(s) => s,
            MaybeTlsStream::Rustls(s) => &mut s.sock,
            _ => unreachable!(),
        };
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        Ok(())
    }
}
pub fn execute(settings: &Value, connection: &Value) -> Result<Value> {
    let mut client = connect(connection, 0)?;
    let socket = &mut client.socket;
    let operation = settings["operation"].as_str().unwrap_or("ToggleRecord");
    let data = settings["data"].as_object().cloned().unwrap_or_default();
    let response = match operation {
        "AdjustInputVolume" => {
            let state = request(
                socket,
                "GetInputVolume",
                json!({"inputName":data.get("inputName")}),
            )?;
            let increment = data
                .get("increment")
                .and_then(Value::as_f64)
                .unwrap_or(0.05);
            let legacy = data.get("volume_curve").and_then(Value::as_str) == Some("legacy");
            ensure!(
                increment.is_finite() && increment.abs() <= if legacy { 100.0 } else { 1.0 },
                "invalid OBS volume change"
            );
            let mut fields = json!({"inputName":data.get("inputName")});
            if legacy {
                let db = state["inputVolumeDb"].as_f64().unwrap_or(-100.0);
                let current = if db < -100.0 {
                    0.0
                } else if db > 0.0 {
                    100.0
                } else {
                    (1.5_f64.powf(db / 10.0) * 100.0).floor()
                };
                let volume = (current + increment).clamp(0.0, 100.0);
                fields["inputVolumeDb"] = json!(if volume == 0.0 {
                    -100.0
                } else {
                    10.0 * (volume / 100.0).ln() / 1.5_f64.ln()
                });
            } else {
                fields["inputVolumeMul"] = json!(
                    (state["inputVolumeMul"]
                        .as_f64()
                        .context("OBS input volume missing")?
                        + increment)
                        .clamp(0.0, 1.0)
                );
            }
            request(socket, "SetInputVolume", fields)?
        }
        "ToggleSceneItemEnabled" | "SetSceneItemEnabled" => {
            let id = request(
                socket,
                "GetSceneItemId",
                json!({"sceneName":data.get("sceneName"),"sourceName":data.get("sourceName")}),
            )?["sceneItemId"]
                .clone();
            ensure!(id.is_number(), "OBS scene item missing");
            let mut fields = json!({"sceneName":data.get("sceneName"),"sceneItemId":id});
            let enabled = if operation == "ToggleSceneItemEnabled" {
                !request(socket, "GetSceneItemEnabled", fields.clone())?["sceneItemEnabled"]
                    .as_bool()
                    .context("OBS scene item state missing")?
            } else {
                data.get("enabled")
                    .and_then(Value::as_bool)
                    .context("scene item enabled state missing")?
            };
            fields["sceneItemEnabled"] = json!(enabled);
            request(socket, "SetSceneItemEnabled", fields)?
        }
        "ToggleSceneFilter" | "SetSceneFilter" => {
            let mut fields =
                json!({"sourceName":data.get("sourceName"),"filterName":data.get("filterName")});
            let enabled = if operation == "ToggleSceneFilter" {
                !request(socket, "GetSourceFilter", fields.clone())?["filterEnabled"]
                    .as_bool()
                    .context("OBS filter state missing")?
            } else {
                data.get("enabled")
                    .and_then(Value::as_bool)
                    .context("filter enabled state missing")?
            };
            fields["filterEnabled"] = json!(enabled);
            request(socket, "SetSourceFilterEnabled", fields)?
        }
        "ToggleStudioMode" => {
            let state = request(socket, "GetStudioModeEnabled", json!({}))?;
            request(
                socket,
                "SetStudioModeEnabled",
                json!({"studioModeEnabled":!state["studioModeEnabled"].as_bool().unwrap_or(false)}),
            )?
        }
        "RecPlayPause" => {
            let state = request(socket, "GetRecordStatus", json!({}))?;
            ensure!(
                state["outputActive"].as_bool() == Some(true),
                "OBS is not recording"
            );
            request(
                socket,
                if state["outputPaused"].as_bool() == Some(true) {
                    "ResumeRecord"
                } else {
                    "PauseRecord"
                },
                json!({}),
            )?
        }
        "TriggerTransition" => request(socket, "TriggerStudioModeTransition", json!({}))?,
        operation => {
            ensure!(
                [
                    "ToggleStream",
                    "StartStream",
                    "StopStream",
                    "ToggleRecord",
                    "StartRecord",
                    "StopRecord",
                    "ToggleReplayBuffer",
                    "StartReplayBuffer",
                    "StopReplayBuffer",
                    "SaveReplayBuffer",
                    "ToggleVirtualCam",
                    "SetCurrentProgramScene",
                    "SetCurrentSceneCollection",
                    "ToggleInputMute",
                    "SetInputMute",
                    "SetInputVolume",
                    "GetStats"
                ]
                .contains(&operation),
                "unsupported OBS operation"
            );
            request(socket, operation, json!(data))?
        }
    };
    let _ = socket.close(None);
    Ok(response)
}
fn request(socket: &mut Socket, request: &str, data: Value) -> Result<Value> {
    request_events(
        socket,
        request,
        data,
        &mut std::collections::HashMap::new(),
        &mut std::collections::HashMap::new(),
    )
}
#[derive(Debug)]
pub struct RequestRejected(pub i64);
impl std::fmt::Display for RequestRejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "OBS request rejected (code {})", self.0)
    }
}
impl std::error::Error for RequestRejected {}
fn request_events(
    socket: &mut Socket,
    request: &str,
    data: Value,
    events: &mut std::collections::HashMap<String, Value>,
    received: &mut std::collections::HashMap<String, Instant>,
) -> Result<Value> {
    socket.send(Message::Text(
        json!({"op":6,"d":{"requestType":request,"requestId":"deckard","requestData":data}})
            .to_string()
            .into(),
    ))?;
    let deadline = Instant::now() + Duration::from_secs(3);
    let response = loop {
        ensure!(
            Instant::now() < deadline && !crate::desktop::cancelled(),
            "OBS request timed out or cancelled"
        );
        match socket.read()? {
            Message::Text(text) => {
                let message: Value = serde_json::from_str(&text)?;
                if message["op"] == 7 {
                    break message["d"].clone();
                }
                if message["op"] == 5
                    && let Some(name) = message["d"]["eventType"].as_str()
                {
                    events.insert(name.into(), message["d"]["eventData"].clone());
                    received.insert(name.into(), Instant::now());
                }
            }
            Message::Close(_) => anyhow::bail!("OBS disconnected"),
            _ => {}
        }
    };
    ensure!(
        response["requestId"] == "deckard",
        "OBS response ID mismatch"
    );
    if response["requestStatus"]["result"].as_bool() != Some(true) {
        return Err(
            RequestRejected(response["requestStatus"]["code"].as_i64().unwrap_or(0)).into(),
        );
    }
    Ok(response["responseData"].clone())
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    #[test]
    fn persistent_readouts_cache_invalidate_on_events_and_survive_bad_selection() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let mut socket = tungstenite::accept(listener.accept().unwrap().0).unwrap();
            socket
                .send(Message::Text(
                    json!({"op":0,"d":{"rpcVersion":1}}).to_string().into(),
                ))
                .unwrap();
            let identify: Value =
                serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
            assert_eq!(identify["d"]["eventSubscriptions"], 1023);
            socket
                .send(Message::Text(json!({"op":2,"d":{}}).to_string().into()))
                .unwrap();
            for (expected, result, code, data) in [
                ("GetStreamStatus", true, 100, json!({"outputActive":false})),
                ("GetStreamStatus", true, 100, json!({"outputActive":true})),
                ("GetInputMute", false, 600, json!({})),
                ("GetStats", true, 100, json!({"cpuUsage":4.0})),
            ] {
                let request: Value =
                    serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
                assert_eq!(request["d"]["requestType"], expected);
                socket.send(Message::Text(json!({"op":7,"d":{"requestId":"deckard","requestStatus":{"result":result,"code":code},"responseData":data}}).to_string().into())).unwrap();
                if expected == "GetStreamStatus" && data["outputActive"] == false {
                    socket.send(Message::Text(json!({"op":5,"d":{"eventType":"StreamStateChanged","eventData":{"outputActive":true}}}).to_string().into())).unwrap();
                }
            }
        });
        let mut c = connect(&json!({"host":"127.0.0.1","port":port}), 1023).unwrap();
        let initial = c.readout("GetStreamStatus", json!({})).unwrap();
        assert_eq!(initial["outputActive"], false);
        assert_eq!(c.readout("GetStreamStatus", json!({})).unwrap(), initial);
        let deadline = Instant::now() + Duration::from_secs(1);
        while !c.events.contains_key("StreamStateChanged") {
            assert!(Instant::now() < deadline, "OBS event was not delivered");
            c.poll().unwrap();
        }
        assert_eq!(c.events["StreamStateChanged"]["outputActive"], true);
        assert_eq!(
            c.readout("GetStreamStatus", json!({})).unwrap()["outputActive"],
            true
        );
        assert!(
            c.query("GetInputMute", json!({"inputName":"Removed"}))
                .unwrap_err()
                .downcast_ref::<RequestRejected>()
                .is_some()
        );
        assert_eq!(c.query("GetStats", json!({})).unwrap()["cpuUsage"], 4.0);
        server.join().unwrap();
    }
    #[test]
    fn authenticated_wire_request_and_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let thread = std::thread::spawn(move || {
            let mut socket = tungstenite::accept(listener.accept().unwrap().0).unwrap();
            socket.send(Message::Text(json!({"op":0,"d":{"rpcVersion":1,"authentication":{"salt":"salt","challenge":"challenge"}}}).to_string().into())).unwrap();
            let identify: Value =
                serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
            assert_eq!(
                identify["d"]["authentication"],
                "zTM5ki6L2vVvBQiTG9ckH1Lh64AbnCf6XZ226UmnkIA="
            );
            socket
                .send(Message::Text(
                    json!({"op":2,"d":{"negotiatedRpcVersion":1}})
                        .to_string()
                        .into(),
                ))
                .unwrap();
            let request: Value =
                serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
            assert_eq!(request["d"]["requestType"], "SetCurrentProgramScene");
            assert_eq!(request["d"]["requestData"]["sceneName"], "Gaming");
            socket.send(Message::Text(json!({"op":7,"d":{"requestId":"deckard","requestStatus":{"result":true,"code":100},"responseData":{}}}).to_string().into())).unwrap();
        });
        execute(
            &json!({"operation":"SetCurrentProgramScene","data":{"sceneName":"Gaming"}}),
            &json!({"host":"127.0.0.1","port":port,"password":"password"}),
        )
        .unwrap();
        thread.join().unwrap();
    }
}
