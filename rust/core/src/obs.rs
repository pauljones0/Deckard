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
pub fn execute(settings: &Value, connection: &Value) -> Result<Value> {
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
    let mut identify = json!({"rpcVersion":1,"eventSubscriptions":0});
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
    let operation = settings["operation"].as_str().unwrap_or("ToggleRecord");
    let data = settings["data"].as_object().cloned().unwrap_or_default();
    let response = match operation {
        "AdjustInputVolume" => {
            let state = request(
                &mut socket,
                "GetInputVolume",
                json!({"inputName":data.get("inputName")}),
            )?;
            let increment = data
                .get("increment")
                .and_then(Value::as_f64)
                .unwrap_or(0.05);
            ensure!(
                increment.is_finite() && (-1.0..=1.0).contains(&increment),
                "invalid OBS volume change"
            );
            let volume = (state["inputVolumeMul"]
                .as_f64()
                .context("OBS input volume missing")?
                + increment)
                .clamp(0.0, 1.0);
            request(
                &mut socket,
                "SetInputVolume",
                json!({"inputName":data.get("inputName"),"inputVolumeMul":volume}),
            )?
        }
        "ToggleSceneItemEnabled" | "SetSceneItemEnabled" => {
            let id = request(
                &mut socket,
                "GetSceneItemId",
                json!({"sceneName":data.get("sceneName"),"sourceName":data.get("sourceName")}),
            )?["sceneItemId"]
                .clone();
            ensure!(id.is_number(), "OBS scene item missing");
            let mut fields = json!({"sceneName":data.get("sceneName"),"sceneItemId":id});
            let enabled = if operation == "ToggleSceneItemEnabled" {
                !request(&mut socket, "GetSceneItemEnabled", fields.clone())?["sceneItemEnabled"]
                    .as_bool()
                    .context("OBS scene item state missing")?
            } else {
                data.get("enabled")
                    .and_then(Value::as_bool)
                    .context("scene item enabled state missing")?
            };
            fields["sceneItemEnabled"] = json!(enabled);
            request(&mut socket, "SetSceneItemEnabled", fields)?
        }
        "ToggleSceneFilter" | "SetSceneFilter" => {
            let mut fields =
                json!({"sourceName":data.get("sourceName"),"filterName":data.get("filterName")});
            let enabled = if operation == "ToggleSceneFilter" {
                !request(&mut socket, "GetSourceFilter", fields.clone())?["filterEnabled"]
                    .as_bool()
                    .context("OBS filter state missing")?
            } else {
                data.get("enabled")
                    .and_then(Value::as_bool)
                    .context("filter enabled state missing")?
            };
            fields["filterEnabled"] = json!(enabled);
            request(&mut socket, "SetSourceFilterEnabled", fields)?
        }
        "ToggleStudioMode" => {
            let state = request(&mut socket, "GetStudioModeEnabled", json!({}))?;
            request(
                &mut socket,
                "SetStudioModeEnabled",
                json!({"studioModeEnabled":!state["studioModeEnabled"].as_bool().unwrap_or(false)}),
            )?
        }
        "RecPlayPause" => {
            let state = request(&mut socket, "GetRecordStatus", json!({}))?;
            ensure!(
                state["outputActive"].as_bool() == Some(true),
                "OBS is not recording"
            );
            request(
                &mut socket,
                if state["outputPaused"].as_bool() == Some(true) {
                    "ResumeRecord"
                } else {
                    "PauseRecord"
                },
                json!({}),
            )?
        }
        "TriggerTransition" => request(&mut socket, "TriggerStudioModeTransition", json!({}))?,
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
            request(&mut socket, operation, json!(data))?
        }
    };
    let _ = socket.close(None);
    Ok(response)
}
fn request(socket: &mut Socket, request: &str, data: Value) -> Result<Value> {
    socket.send(Message::Text(
        json!({"op":6,"d":{"requestType":request,"requestId":"deckard","requestData":data}})
            .to_string()
            .into(),
    ))?;
    let response = receive(socket, 7)?;
    ensure!(
        response["requestId"] == "deckard",
        "OBS response ID mismatch"
    );
    ensure!(
        response["requestStatus"]["result"].as_bool() == Some(true),
        "OBS request failed (code {})",
        response["requestStatus"]["code"]
    );
    Ok(response["responseData"].clone())
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
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
